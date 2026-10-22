import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from launch_config import (
    config_directory, config_environments, datasource_properties, join_arguments,
    load_config, parse_environment, parse_jvmparams, prepare_backend, resolve_values,
    split_arguments,
)
from project_discovery import discover_toolchains, find_spring_boot_classes


class ConfigFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="launcher config ")
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name)
        self.common = self.root_path / "repo" / "test" / "common"
        self.common.mkdir(parents=True)
        self.app_directory = self.root_path / "application data"
        self.app_directory.mkdir()
        (self.common / "thyapp.jvmparams.txt").write_text(
            'LOG_DIR="${thy.appdir}/service/log"\n'
            'CONF_DIR="${thy.appdir}/service/conf"\n'
            'JAVA_OPTIONS= \\\n'
            ' -Dspring.config.additional-location=file:${CONF_DIR}/application.yml \\\n'
            ' -Dthy.listenport=21007 \\\n'
            ' -Dthy.bagglobe-rest.listenport="21009" \\\n'
            ' -Dapp.integrations.location="file:${CONF_DIR}/integrations.xml" \\\n'
            ' -XX:ErrorFile="${LOG_DIR}/hs_err_pid_%p.log"\n', encoding="utf-8")
        (self.common / "application.yml").write_text(
            'server:\n  port: ${thy.listenport:8080}\n'
            'spring:\n  datasource:\n    primary:\n'
            '      jdbc-url: jdbc:postgresql://example/config\n'
            '      password: original\n'
            '    read-only-database:\n      jdbc-url: jdbc:postgresql://example/readonly\n'
            'feature:\n  enabled: true\n  timeout: ${custom.timeout:30}\n', encoding="utf-8")

    def prepare(self, **kwargs):
        return prepare_backend(str(self.common.parent), "", str(self.app_directory), **kwargs)


class ConfigTests(ConfigFixture):
    def test_jvmparams_continuations_and_properties(self):
        env, props, flags = parse_jvmparams((self.common / "thyapp.jvmparams.txt").read_text())
        self.assertEqual(props["thy.bagglobe-rest.listenport"], "21009")
        self.assertEqual(env["CONF_DIR"], "${thy.appdir}/service/conf")
        self.assertEqual(flags, ["-XX:ErrorFile=${LOG_DIR}/hs_err_pid_%p.log"])

    def test_windows_paths_and_quotes(self):
        _, props, _ = parse_jvmparams('JAVA_OPTIONS=-Dpath="C:\\Apps With Spaces\\conf"')
        self.assertEqual(props["path"], r"C:\Apps With Spaces\conf")

    def test_java_opts_can_wrap_the_entire_option_block_in_quotes(self):
        _, props, flags = parse_jvmparams('JAVA_OPTS=\'-Dpath="C:/Apps With Spaces" -Dport=19000 -Xmx512m\'')
        self.assertEqual(props, {"path": "C:/Apps With Spaces", "port": "19000"})
        self.assertEqual(flags, ["-Xmx512m"])

    def test_raw_options_comments_and_bom(self):
        _, props, flags = parse_jvmparams('\ufeff# comment\n-Dflag -Dnumber=7 # comment\n-Xmx512m\n')
        self.assertEqual(props, {"flag": "", "number": "7"})
        self.assertEqual(flags, ["-Xmx512m"])

    def test_no_shell_execution(self):
        with self.assertRaises(ValueError):
            parse_jvmparams("rm -rf /tmp/anything")

    def test_environment_preserves_values(self):
        self.assertEqual(parse_environment('FLAG="hello world"\nTOKEN=a=b\nEMPTY=\n# comment'),
                         {"FLAG": "hello world", "TOKEN": "a=b", "EMPTY": ""})
        with self.assertRaises(ValueError):
            parse_environment("invalid env=value")

    def test_repo_and_common_discovery(self):
        server = self.common.parent / "oapptest21"
        server.mkdir()
        (server / "thyapp.jvmparams.txt").write_text("-Dignored=true")
        self.assertEqual(config_environments(str(self.common.parent.parent)), ["test"])
        self.assertEqual(config_environments(str(self.common.parent)), [""])
        self.assertEqual(config_directory(str(self.common.parent)), self.common)
        self.assertNotIn("ignored", load_config(str(self.common.parent))["properties"])

    def test_only_jvm_option_properties_are_editable(self):
        with (self.common / "thyapp.jvmparams.txt").open("a", encoding="utf-8") as stream:
            stream.write("JAVA_OPTS=-Dextra.port=19000\n-Draw.property=hidden\n")
        loaded = load_config(str(self.common))
        self.assertEqual(loaded["fields"]["thy.listenport"]["value"], "21007")
        self.assertEqual(loaded["fields"]["extra.port"]["value"], "19000")
        for key in ("custom.timeout", "feature.enabled", "CONF_DIR", "LOG_DIR", "raw.property", "thy.appdir"):
            self.assertNotIn(key, loaded["fields"])
        self.assertEqual(loaded["readonly_prefix"], "spring.datasource.read-only-database")

    def test_properties_standard_escaping(self):
        (self.common / "application.properties").write_bytes(
            b"server.port=${thy.listenport:8080}\nlabel=hello\\ world\\u0021\n"
            b"spring.datasource.read-only.jdbc-url=jdbc:test\n")
        loaded = load_config(str(self.common), "application.properties")
        self.assertEqual(loaded["application"]["label"], "hello world!")
        self.assertEqual(loaded["readonly_prefix"], "spring.datasource.read-only")

    def test_yaml_multidocument_and_lists(self):
        (self.common / "application.yaml").write_text(
            "routes:\n - host: example\n---\nspring:\n config:\n  activate:\n   on-profile: dev\n", encoding="utf-8")
        loaded = load_config(str(self.common), "application.yaml")
        self.assertEqual(loaded["application"]["routes[0].host"], "example")

    def test_override_precedence_and_environment(self):
        _, props, flags, env = self.prepare(
            vm_options="-Dthy.listenport=18000 -Xmx512m",
            overrides={"thy.listenport": "19000", "CONF_DIR": str(self.common)},
            environment_text="EXTRA=hello world")
        self.assertEqual(props["thy.listenport"], "19000")
        self.assertEqual(env["thy.listenport"], "19000")
        self.assertEqual(env["THY_LISTENPORT"], "19000")
        self.assertEqual(env["EXTRA"], "hello world")
        self.assertEqual(props["spring.config.additional-location"], (self.common / "application.yml").as_uri())
        self.assertIn("-Xmx512m", flags)
        self.assertIn(self.app_directory.as_posix(), flags[0])

    def test_no_datasource_selection_keeps_yaml_authoritative(self):
        _, props, _, _ = self.prepare()
        self.assertFalse(any(key.startswith("spring.datasource.") for key in props))
        self.assertEqual(datasource_properties(None, "spring.datasource.primary"), {})

    def test_datasource_empty_password_and_whitespace(self):
        props = datasource_properties({"password": "", "username": " user "}, "spring.datasource.primary")
        self.assertEqual(props["spring.datasource.primary.password"], "")
        self.assertEqual(props["spring.datasource.primary.username"], " user ")

    def test_required_appdir_and_missing_jvmparams(self):
        with self.assertRaises(ValueError):
            prepare_backend(str(self.common), "", "")
        (self.common / "thyapp.jvmparams.txt").unlink()
        with self.assertRaises(ValueError):
            self.prepare()

    def test_arbitrary_config_folder_works_without_jvmparams(self):
        custom = self.root_path / "custom-config"
        self.common.rename(custom)
        (custom / "thyapp.jvmparams.txt").unlink()
        self.assertEqual(config_directory(custom), custom)
        self.assertEqual(config_environments(custom), [""])
        loaded = load_config(custom)
        self.assertTrue(loaded["requires_ports"])
        self.assertEqual(loaded["fields"], {})
        loaded, props, _, env = prepare_backend(custom, "", str(self.app_directory),
                                                listen_port="21007", metric_port="22007")
        self.assertEqual(props["thy.listenport"], "21007")
        self.assertEqual(props["thy.metricport"], "22007")
        self.assertEqual(env["THY_LISTENPORT"], "21007")
        self.assertEqual(env["THY_METRICPORT"], "22007")

    def test_direct_config_is_preferred_over_a_common_subfolder(self):
        (self.common.parent / "application.properties").write_text("direct=true\n", encoding="utf-8")
        self.assertEqual(config_directory(self.common.parent), self.common.parent)
        self.assertEqual(load_config(self.common.parent)["application"], {"direct": "true"})

    def test_environment_discovery_does_not_require_jvmparams(self):
        (self.common / "thyapp.jvmparams.txt").unlink()
        direct = self.common.parent.parent / "local"
        direct.mkdir()
        (direct / "application.yaml").write_text("feature: true\n", encoding="utf-8")
        self.assertEqual(config_environments(self.common.parent.parent), ["local", "test"])

    def test_jvmparams_filename_alias_is_supported(self):
        (self.common / "thyapp.jvmparams.txt").rename(self.common / "jvmparams.txt")
        loaded = load_config(self.common)
        self.assertFalse(loaded["requires_ports"])
        self.assertEqual(loaded["fields"]["thy.listenport"]["source"], "jvmparams.txt")
        self.prepare()

    def test_missing_jvmparams_requires_both_valid_ports(self):
        (self.common / "thyapp.jvmparams.txt").unlink()
        for listen, metric in (("", "22007"), ("21007", ""), ("0", "22007"),
                               ("21007", "65536"), ("abc", "22007")):
            with self.subTest(listen=listen, metric=metric), self.assertRaises(ValueError):
                self.prepare(listen_port=listen, metric_port=metric)

    def test_manual_ports_do_not_override_an_existing_jvmparams_file(self):
        _, props, _, _ = self.prepare(listen_port="19000", metric_port="bad")
        self.assertEqual(props["thy.listenport"], "21007")
        self.assertNotIn("thy.metricport", props)

    def test_unresolved_placeholder_is_rejected(self):
        with self.assertRaises(ValueError):
            self.prepare(overrides={"broken": "${MISSING_LAUNCHER_VALUE}"})

    def test_nested_placeholder_defaults(self):
        self.assertEqual(resolve_values({"key": "${ABSENT:${OTHER:hello}}"}, {})["key"], "hello")

    def test_paths_and_commas_are_not_split(self):
        values = ["-Dpath=C:/Apps With Spaces/conf", "--spring.profiles.active=dev,local", "--value="]
        self.assertEqual(split_arguments(join_arguments(values)), values)

    def test_source_files_are_unchanged(self):
        before = {path: hashlib.sha256(path.read_bytes()).digest() for path in self.common.iterdir()}
        self.prepare(overrides={"server.port": "19999"})
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).digest() for path in self.common.iterdir()})

    def test_existing_companion_files_are_relocated_without_copying(self):
        with (self.common / "application.yml").open("a", encoding="utf-8") as stream:
            stream.write("app:\n  firebase:\n    service-account: file:${thy.appdir}/service/conf/google-services.json\n")
        companion = self.common / "google-services.json"
        companion.write_text("{}", encoding="utf-8")
        loaded, props, _, _ = self.prepare()
        self.assertEqual(props["app.firebase.service-account"], companion.as_uri())
        self.assertIn("app.firebase.service-account", loaded["resource_properties"])
        self.assertFalse((self.app_directory / "service").exists())
        _, props, _, _ = self.prepare(overrides={"app.firebase.service-account": "file:/custom/service.json"})
        self.assertEqual(props["app.firebase.service-account"], "file:/custom/service.json")


class DiscoveryTests(ConfigFixture):
    def write_source(self, relative, source):
        path = self.root_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def test_main_class_uses_annotation_and_package(self):
        self.write_source("src/main/java/demo/Boot.java", 'package demo.service;\n'
                          'class Helper {}\n@SpringBootApplication(scanBasePackages = "demo")\npublic class Boot {}')
        self.assertEqual(find_spring_boot_classes(self.root_path), ["demo.service.Boot"])

    def test_test_and_generated_sources_are_excluded(self):
        for folder in ("src/test", "src/Test", "tests", "target/generated-sources", "build", "node_modules"):
            self.write_source(f"{folder}/Wrong.java", "@SpringBootApplication class Wrong {}")
        self.write_source("src/main/java/Real.java", "@SpringBootApplication class Real {}")
        self.assertEqual(find_spring_boot_classes(self.root_path), ["Real"])

    def test_comments_and_string_literals_are_not_annotations(self):
        self.write_source("src/main/java/Other.java", '// @SpringBootApplication class Fake {}\n'
                          '/* @SpringBootApplication class Fake2 {} */\n'
                          'class Other { String text = "@SpringBootApplication class Fake3 {}"; }')
        self.assertEqual(find_spring_boot_classes(self.root_path), [])

    def test_multiple_modules_and_qualified_annotation(self):
        for name in ("one", "two"):
            self.write_source(f"{name}/src/main/java/App.java", f"package {name};\n"
                              "@org.springframework.boot.autoconfigure.SpringBootApplication public class App {}")
        self.assertEqual(find_spring_boot_classes(self.root_path), ["one.App", "two.App"])

    def test_tools_from_environment_path_and_siblings(self):
        tools = self.root_path / "tools"
        for home, executable in (("jdk-17", "java.exe"), ("jdk-21", "java.exe"), ("apache-maven-3.9", "mvn.cmd")):
            binary = tools / home / "bin" / executable
            binary.parent.mkdir(parents=True)
            binary.touch()
            if executable == "java.exe":
                (binary.parent / "javac.exe").touch()
        env = {"JAVA_HOME": str(tools / "jdk-21"), "MAVEN_HOME": str(tools / "apache-maven-3.9"),
               "PATH": os.pathsep.join([str(tools / "jdk-21" / "bin"), str(tools / "apache-maven-3.9" / "bin")])}
        result = discover_toolchains(env, [])
        self.assertEqual(result["java"], [str(tools / "jdk-17"), str(tools / "jdk-21")])
        self.assertEqual(result["maven"], [str(tools / "apache-maven-3.9")])

    def test_invalid_tools_and_unknown_folders_are_ignored(self):
        result = discover_toolchains({"JAVA_HOME": str(self.root_path / "missing"), "PATH": ""}, [self.root_path])
        self.assertEqual(result, {"java": [], "maven": []})

    def test_java_runtime_without_a_compiler_is_not_offered_as_a_jdk(self):
        binary = self.root_path / "jre" / "bin" / "java.exe"
        binary.parent.mkdir(parents=True)
        binary.touch()
        self.assertEqual(discover_toolchains({"JAVA_HOME": str(binary.parent.parent)}, [self.root_path])["java"], [])


class LauncherTests(ConfigFixture):
    def setUp(self):
        super().setUp()
        from app_launcher import AppLauncher
        self.launcher_class = AppLauncher
        self.project_config = {
            "name": "Service", "project_path": str(self.root_path),
            "config_repo_path": str(self.common.parent.parent), "config_environment": "test",
            "app_directory": str(self.app_directory), "environment_variables": "EXTRA=hello world",
            "property_overrides": {"thy.listenport": "19001"},
            "listen_port": "21007", "metric_port": "22007",
        }
        self.datasource = {"id": "primary-test", "name": "Test DB", "category": "primary", "environment": "local",
                           "jdbc_url": "jdbc:postgresql://example/ui", "username": "demo", "password": "secret value"}
        with patch.object(AppLauncher, "_load_settings_file", return_value={"tabs": [{"config": self.project_config}]}), \
                patch.object(AppLauncher, "_load_datasources_file", return_value=[self.datasource]):
            self.app = AppLauncher()
        self.app.withdraw()
        self.tab = next(iter(self.app.tabs.values()))["project"]
        self.tab.process_manager.run = Mock(return_value=True)
        self.addCleanup(self.close_app)

    def close_app(self):
        for info in self.app.tabs.values():
            info["project"].destroy()
        for job in self.app.tk.call("after", "info"):
            self.app.tk.call("after", "cancel", job)
        self.app.destroy()

    def test_backend_launch_and_web_exemption(self):
        self.tab.run_app()
        self.tab.process_manager.run.assert_called_once()
        self.assertEqual(self.tab.process_manager.run.call_args.kwargs["env"]["THY_LISTENPORT"], "19001")
        self.tab.setting_vars["app_directory"].set("")
        self.tab.run_app()
        self.assertEqual(self.tab.process_manager.run.call_count, 1)
        self.tab.setting_vars["project_type"].set("npm")
        self.tab.run_app()
        self.assertEqual(self.tab.process_manager.run.call_count, 2)

    def test_datasource_and_port_win_over_program_arguments(self):
        self.tab.setting_vars["primary_datasource_id"].set(self.app.get_datasource_label("primary-test", "primary"))
        self.tab.setting_vars["program_args"].set('--spring.datasource.primary.password=old --server.port=8080 --tags=one,two')
        self.tab.setting_vars["override_port"].set(True)
        self.tab.setting_vars["server_port"].set("19999")
        self.tab.run_app()
        command = self.tab.process_manager.run.call_args.args[0]
        arguments = next(part.split("=", 1)[1] for part in command if part.startswith("-Dspring-boot.run.arguments="))
        tokens = split_arguments(arguments)
        self.assertIn("--spring.datasource.primary.password=secret value", tokens)
        self.assertIn("--server.port=19999", tokens)
        self.assertIn("--tags=one,two", tokens)
        self.assertNotIn("--server.port=8080", tokens)
        self.assertNotIn("secret value", self.tab._command_for_log(command))
        self.assertNotIn("=old", self.tab._command_for_log(command))

    def test_override_editor_and_persistence(self):
        self.tab._selected_property = "thy.bagglobe-rest.listenport"
        self.tab._refresh_property_list()
        self.tab.property_enabled.set(True)
        self.tab._store_property_override()
        self.tab.property_value.set("19901")
        config = self.tab.get_config()
        self.assertEqual(config["property_overrides"]["thy.bagglobe-rest.listenport"], "19901")
        self.assertEqual(config["environment_variables"], "EXTRA=hello world")
        self.tab.property_enabled.set(False)
        self.tab._store_property_override()
        self.assertNotIn("thy.bagglobe-rest.listenport", self.tab.get_config()["property_overrides"])

    def test_legacy_yaml_overrides_are_not_hidden_active_settings(self):
        self.tab.property_overrides["feature.enabled"] = "false"
        self.tab._load_config_fields()
        self.assertNotIn("feature.enabled", self.tab._visible_properties)
        self.assertNotIn("feature.enabled", self.tab.get_config()["property_overrides"])

    def test_missing_jvmparams_exposes_required_ports_and_blocks_invalid_launch(self):
        params = self.common / "thyapp.jvmparams.txt"
        original = params.read_text(encoding="utf-8")
        params.unlink()
        self.tab._refresh_config()
        self.assertTrue(self.tab.fallback_ports_frame.grid_info())
        self.tab.setting_vars["listen_port"].set("")
        self.tab.setting_vars["metric_port"].set("")
        self.tab.run_app()
        self.tab.process_manager.run.assert_not_called()
        self.tab.setting_vars["listen_port"].set("21001")
        self.tab.run_app()
        self.tab.process_manager.run.assert_not_called()
        self.tab.setting_vars["metric_port"].set("22001")
        self.tab.setting_vars["program_args"].set("--thy.listenport=1 --thy.metricport=2")
        self.tab.run_app()
        self.tab.process_manager.run.assert_called_once()
        call = self.tab.process_manager.run.call_args
        self.assertEqual(call.kwargs["env"]["THY_LISTENPORT"], "21001")
        self.assertEqual(call.kwargs["env"]["THY_METRICPORT"], "22001")
        arguments = next(argument.split("=", 1)[1] for argument in call.args[0]
                         if argument.startswith("-Dspring-boot.run.arguments="))
        self.assertIn("--thy.listenport=21001", split_arguments(arguments))
        self.assertIn("--thy.metricport=22001", split_arguments(arguments))
        self.assertNotIn("--thy.listenport=1", split_arguments(arguments))
        params.write_text(original, encoding="utf-8")
        self.tab._load_config_fields()
        self.assertFalse(self.tab.fallback_ports_frame.grid_info())

    def test_direct_folder_selection_does_not_append_common(self):
        from app_launcher import SELECTED_CONFIG_FOLDER
        custom = self.root_path / "arbitrary-config"
        self.common.rename(custom)
        self.tab.setting_vars["config_repo_path"].set(str(custom))
        self.tab._refresh_config()
        self.assertEqual(self.tab.setting_vars["config_environment"].get(), SELECTED_CONFIG_FOLDER)
        self.assertEqual(self.tab.loaded_config["directory"], custom)
        self.assertEqual(self.tab.get_config()["config_environment"], "")
        self.tab.run_app()
        self.tab.process_manager.run.assert_called_once()

    def test_web_does_not_require_ports_when_jvmparams_is_missing(self):
        (self.common / "thyapp.jvmparams.txt").unlink()
        self.tab.setting_vars["listen_port"].set("")
        self.tab.setting_vars["metric_port"].set("")
        self.tab.setting_vars["project_type"].set("npm")
        self.tab._load_config_fields()
        self.assertFalse(self.tab.fallback_ports_frame.grid_info())
        self.tab.run_app()
        self.tab.process_manager.run.assert_called_once()

    def test_legacy_common_environment_sentinel_is_migrated(self):
        self.tab.setting_vars["config_repo_path"].set(str(self.common))
        self.tab.setting_vars["config_environment"].set("common")
        self.tab._refresh_config()
        self.assertEqual(self.tab.loaded_config["directory"], self.common)

    def test_selected_project_automatically_sets_main_class(self):
        source = self.root_path / "src" / "main" / "java" / "Demo.java"
        source.parent.mkdir(parents=True)
        source.write_text("package example; @SpringBootApplication public class Demo {}", encoding="utf-8")
        self.tab._start_main_class_scan()
        self.tab._main_class_thread.join(timeout=5)
        self.tab._consume_main_class_results()
        self.assertEqual(self.tab.setting_vars["main_class"].get(), "example.Demo")
        self.tab.run_app()
        self.assertIn("-Dspring-boot.run.mainClass=example.Demo", self.tab.process_manager.run.call_args.args[0])

    def test_main_class_results_from_previous_project_are_ignored(self):
        self.tab._main_class_results.put((self.tab._main_scan_generation - 1, str(self.root_path), ["old.Application"]))
        self.tab._consume_main_class_results()
        self.assertNotEqual(self.tab.setting_vars["main_class"].get(), "old.Application")

    def test_changing_project_immediately_clears_the_previous_main_class(self):
        self.tab.setting_vars["main_class"].set("old.Application")
        self.tab.setting_vars["project_path"].set(str(self.root_path / "another-project"))
        self.assertEqual(self.tab.setting_vars["main_class"].get(), "")

    def test_multiple_main_classes_populate_dropdown_without_guessing(self):
        self.tab.setting_vars["main_class"].set("old.Application")
        candidates = ["one.Application", "two.Application"]
        self.tab._main_class_results.put((self.tab._main_scan_generation, str(self.root_path), candidates))
        self.tab._consume_main_class_results()
        self.assertEqual(self.tab.setting_vars["main_class"].get(), "")
        self.assertEqual(self.tab.main_class_menu.cget("values"), candidates)

    def test_toolchain_dropdown_selection_and_saved_defaults(self):
        java_home = self.root_path / "jdk-21"
        maven_home = self.root_path / "apache-maven-3.9"
        for home, executable in ((java_home, "java.exe"), (maven_home, "mvn.cmd")):
            binary = home / "bin" / executable
            binary.parent.mkdir(parents=True)
            binary.touch()
        self.app.toolchains = {"java": [str(java_home)], "maven": [str(maven_home)]}
        self.tab.refresh_toolchain_options()
        self.assertIn(str(java_home), self.tab.toolchain_menus["java"].cget("values"))
        self.assertIn(str(maven_home), self.tab.toolchain_menus["maven"].cget("values"))
        self.tab.setting_vars["java_path"].set(str(java_home))
        self.tab.setting_vars["maven_path"].set(str(maven_home))
        self.assertEqual(self.tab._get_java_cmd(), str(java_home / "bin" / "java.exe"))
        self.assertEqual(self.tab._get_maven_cmd(), str(maven_home / "bin" / "mvn.cmd"))
        self.assertEqual(self.tab._get_env()["JAVA_HOME"], str(java_home))
        self.assertEqual(self.tab.get_config()["maven_path"], str(maven_home))
        from app_launcher import SYSTEM_TOOLCHAIN
        self.tab.setting_vars["java_path"].set(SYSTEM_TOOLCHAIN)
        self.assertEqual(self.tab.get_config()["java_path"], "java")

    def test_export_import_round_trip(self):
        backup = self.root_path / "backup.json"
        with patch("app_launcher.filedialog.asksaveasfilename", return_value=str(backup)), \
                patch("app_launcher.messagebox.showinfo"):
            self.app._export_settings()
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["schema_version"], 2)
        with patch("app_launcher.filedialog.askopenfilename", return_value=str(backup)), \
                patch("app_launcher.messagebox.showinfo"), \
                patch("app_launcher.SETTINGS_FILE", str(self.root_path / "saved.json")), \
                patch("app_launcher.DATASOURCES_FILE", str(self.root_path / "datasources.json")):
            self.app._import_settings()
        restored = next(iter(self.app.tabs.values()))["project"].get_config()
        for key in ("config_repo_path", "config_environment", "app_directory", "property_overrides", "environment_variables",
                "listen_port", "metric_port"):
            self.assertEqual(restored[key], self.project_config[key])
        self.assertEqual(self.app.datasources[0]["password"], "secret value")
        self.assertTrue((self.root_path / "saved.json").is_file())

    def test_old_settings_get_new_defaults(self):
        from app_launcher import ProjectTab
        self.assertEqual(ProjectTab._default_config(self.tab)["property_overrides"], {})
        self.assertEqual(self.tab.get_config()["config_filename"], "application.yml")

    def test_compact_override_editor_stays_visible(self):
        self.app.geometry("1000x700")
        self.app.deiconify()
        self.app.update()
        self.app._toggle_theme()
        self.tab.settings_views.set("Overrides")
        self.app.update_idletasks()
        canvas = self.tab.settings_pages["Overrides"]._parent_canvas
        self.assertGreater(canvas.winfo_height(), 100)
        self.assertLessEqual(self.tab.property_entry.winfo_rooty() + self.tab.property_entry.winfo_height(),
                             canvas.winfo_rooty() + canvas.winfo_height())
        self.assertEqual(self.tab.property_source.cget("bg_color"), ("gray83", "gray17"))


if __name__ == "__main__":
    unittest.main()