import os
import re
import shlex
from pathlib import Path


PLACEHOLDER = re.compile(r"\$\{([^{}]+)\}")
SECRET = re.compile(r"password|passwd|secret|token|credential|api[._-]?key", re.I)


def split_arguments(text):
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    lexer.escape = ""
    return list(lexer)


def parse_jvmparams(text, *, blocks_only=False):
    text = re.sub(r"\\\s*\r?\n", " ", text.lstrip("\ufeff"))
    environment = {}
    options = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assignment = re.match(r"(?:export\s+)?([A-Za-z_][\w]*)\s*=(.*)", line)
        if assignment:
            key, value = assignment.groups()
            tokens = split_arguments(value)
            if key in ("JAVA_OPTIONS", "JAVA_OPTS", "JVM_OPTIONS", "JVM_OPTS"):
                if len(tokens) == 1 and value.strip().startswith(("'", '"')):
                    tokens = split_arguments(tokens[0])
                options.extend(tokens)
            else:
                environment[key] = " ".join(tokens)
        elif not blocks_only:
            tokens = split_arguments(line)
            if any(not token.startswith("-") for token in tokens):
                raise ValueError("Unsupported statement in thyapp.jvmparams.txt")
            options.extend(tokens)
    properties = {}
    flags = []
    for option in options:
        if option.startswith("-D"):
            key, separator, value = option[2:].partition("=")
            if not key:
                raise ValueError("Empty JVM property name")
            properties[key] = value if separator else ""
        else:
            flags.append(option)
    return environment, properties, flags


def parse_environment(text):
    result = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise ValueError(f"Environment line {line_number}: expected NAME=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key.strip()] = value
    return result


def resolve_values(values, inherited=None):
    resolved = dict(values)
    context = dict(os.environ if inherited is None else inherited)
    for _ in range(30):
        changed = False
        context.update(resolved)
        for key, value in resolved.items():
            def replace(match):
                name, _, default = match.group(1).partition(":")
                return str(context.get(name, default if ":" in match.group(1) else match.group(0)))
            expanded = PLACEHOLDER.sub(replace, str(value))
            if expanded != value:
                resolved[key] = expanded
                changed = True
        if not changed:
            return resolved
    raise ValueError("Circular or excessively nested configuration placeholders")


def _application_files(path):
    if not path.is_dir():
        return []
    return sorted(child.name for child in path.iterdir()
                  if child.is_file() and child.name.startswith("application")
                  and child.suffix.lower() in (".yml", ".yaml", ".properties"))


def config_directory(folder):
    path = Path(folder).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("Config folder does not exist")
    if not _application_files(path) and _application_files(path / "common"):
        path = path / "common"
    return path


def config_environments(folder):
    path = config_directory(folder)
    if _application_files(path):
        return [""]
    return sorted(child.name for child in path.iterdir()
                  if child.is_dir() and config_files(child))


def config_files(folder):
    path = config_directory(folder)
    return _application_files(path)


def _flatten(value, prefix=""):
    result = {}
    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(child, name))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.update(_flatten(child, f"{prefix}[{index}]"))
    else:
        result[prefix] = "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
    return result


def load_config(folder, filename=""):
    directory = config_directory(folder)
    files = config_files(directory)
    if not files:
        raise ValueError("No application.yml, application.yaml or application.properties found")
    if not filename:
        filename = next((name for name in ("application.yml", "application.yaml", "application.properties")
                         if name in files), files[0])
    if filename not in files:
        raise ValueError("Selected application config file no longer exists")
    params_file = next((directory / name for name in ("thyapp.jvmparams.txt", "jvmparams.txt")
                        if (directory / name).is_file()), None)
    params_text = params_file.read_text(encoding="utf-8-sig") if params_file else ""
    environment, properties, flags = parse_jvmparams(params_text)
    _, editable_properties, _ = parse_jvmparams(params_text, blocks_only=True)
    config_path = directory / filename
    if config_path.suffix == ".properties":
        import javaproperties
        with config_path.open("rb") as stream:
            application = dict(javaproperties.load(stream))
    else:
        import yaml
        application = {}
        with config_path.open(encoding="utf-8-sig") as stream:
            for document in yaml.safe_load_all(stream):
                if document is not None and not isinstance(document, dict):
                    raise ValueError("Application YAML must contain a mapping")
                application.update(_flatten(document or {}))
    fields = {key: {"value": value, "source": params_file.name if params_file else ""}
              for key, value in editable_properties.items() if key != "thy.appdir"}
    readonly_prefix = "spring.datasource.read-only-database"
    if any(key.startswith("spring.datasource.read-only.") for key in application):
        readonly_prefix = "spring.datasource.read-only"
    return {"directory": directory, "path": config_path, "filename": filename,
            "params_path": params_file, "requires_ports": params_file is None,
            "environment": environment, "properties": properties, "flags": flags,
            "fields": fields, "application": application, "readonly_prefix": readonly_prefix}


def datasource_properties(datasource, prefix):
    if not datasource:
        return {}
    result = {}
    for source, target in (("jdbc_url", "jdbc-url"), ("username", "username"),
                           ("password", "password"), ("driver_class_name", "driver-class-name")):
        value = datasource.get(source)
        if value is not None:
            result[f"{prefix}.{target}"] = str(value)
    return result


def prepare_backend(folder, filename, app_directory, overrides=None, environment_text="", vm_options="",
                    *, listen_port="", metric_port=""):
    if not app_directory or not Path(app_directory).is_dir():
        raise ValueError("Backend requires an existing thy.appdir folder")
    loaded = load_config(folder, filename)
    required_properties = {}
    if loaded["requires_ports"]:
        for key, value in (("thy.listenport", listen_port), ("thy.metricport", metric_port)):
            port = str(value).strip()
            if not re.fullmatch(r"[0-9]+", port) or not 1 <= int(port) <= 65535:
                raise ValueError(f"{key} is required without JVM params and must be a port between 1 and 65535")
            required_properties[key] = str(int(port))
    loaded["required_properties"] = required_properties
    properties = dict(loaded["properties"])
    resources = {}
    for key, value in {**loaded["application"], **properties}.items():
        if value.startswith("file:"):
            basename = value.replace("\\", "/").rsplit("/", 1)[-1]
            candidate = loaded["directory"] / basename
            if basename and candidate.is_file():
                resources[key] = candidate.as_uri()
    properties.update(resources)
    loaded["resource_properties"] = resources
    flags = list(loaded["flags"])
    for token in split_arguments(vm_options):
        if token.startswith("-D"):
            key, _, value = token[2:].partition("=")
            properties[key] = value
        else:
            flags.append(token)
    properties["spring.config.additional-location"] = loaded["path"].as_uri()
    properties.update(overrides or {})
    properties.update(required_properties)
    properties["thy.appdir"] = Path(app_directory).resolve().as_posix()
    environment = dict(loaded["environment"])
    environment["CONF_DIR"] = loaded["directory"].as_posix()
    environment.update(parse_environment(environment_text))
    context = resolve_values({**environment, **properties, "thy.appdir": properties["thy.appdir"]})
    properties = {key: context[key] for key in properties}
    environment = {key: context[key] for key in environment}
    for key, value in properties.items():
        environment.setdefault(key, value)
        normalized = re.sub(r"[^A-Za-z0-9]", "_", key).upper()
        environment.setdefault(normalized, value)
    flags = [resolve_values({"flag": flag}, context)["flag"] for flag in flags]
    for value in list(properties.values()) + list(environment.values()) + flags:
        if PLACEHOLDER.search(value):
            raise ValueError("Unresolved JVM/env placeholder; set its property or environment value")
    return loaded, properties, flags, environment


def join_arguments(arguments):
    return " ".join('"' + argument.replace('"', '\\"') + '"'
                    if not argument or any(char.isspace() or char == '"' for char in argument)
                    else argument for argument in arguments)