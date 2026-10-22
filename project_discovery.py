import os
import re
from collections import deque
from pathlib import Path


JAVA_NON_CODE = re.compile(
    r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\r\n]*|/\*[\s\S]*?\*/'
)
BOOT_ANNOTATION = re.compile(r"@(?:org\.springframework\.boot\.autoconfigure\.)?SpringBootApplication\b")
SOURCE_EXCLUDES = {
    "test", "tests", "testfixtures", "test-fixtures", "integration-test", "integration-tests",
    "target", "build", "out", "generated", "generated-sources", ".git", ".gradle", ".idea",
    "node_modules", ".venv", "venv",
}


def find_spring_boot_classes(folder):
    path = Path(folder)
    if not path.is_dir():
        return []
    candidates = set()
    for directory, subdirectories, files in os.walk(path):
        subdirectories[:] = [name for name in subdirectories if name.lower() not in SOURCE_EXCLUDES]
        for filename in files:
            if not filename.endswith(".java"):
                continue
            try:
                source = (Path(directory) / filename).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            code = JAVA_NON_CODE.sub(lambda match: " " * len(match.group()), source)
            package = re.search(r"\bpackage\s+([\w$.]+)\s*;", code)
            for annotation in BOOT_ANNOTATION.finditer(code):
                declaration = re.search(r"\bclass\s+([A-Za-z_$][\w$]*)", code[annotation.end():])
                if declaration:
                    name = declaration.group(1)
                    candidates.add(f"{package.group(1)}.{name}" if package else name)
    return sorted(candidates)


def _tool_home(candidate, kind):
    try:
        path = Path(candidate).expanduser().resolve()
        if path.is_file():
            path = path.parent
        if path.name.lower() == "bin":
            path = path.parent
        executables = ("java.exe", "java") if kind == "java" else ("mvn.cmd", "mvn.exe", "mvn.bat", "mvn")
        if kind == "java" and not any((path / "bin" / name).is_file() for name in ("javac.exe", "javac")):
            return None
        return str(path) if any((path / "bin" / name).is_file() for name in executables) else None
    except (OSError, ValueError):
        return None


def discover_toolchains(environment=None, search_roots=None):
    environment = dict(os.environ if environment is None else environment)
    discovered = {"java": set(), "maven": set()}
    roots = []
    for name, kind in (("JAVA_HOME", "java"), ("JDK_HOME", "java"),
                       ("MAVEN_HOME", "maven"), ("M2_HOME", "maven")):
        value = environment.get(name)
        if value:
            home = _tool_home(value, kind)
            if home:
                discovered[kind].add(home)
                roots.append(Path(home).parent)
    for directory in environment.get("PATH", environment.get("Path", "")).split(os.pathsep):
        if directory.strip():
            for kind in discovered:
                home = _tool_home(directory.strip().strip('"'), kind)
                if home:
                    discovered[kind].add(home)
                    roots.append(Path(home).parent)
    if search_roots is None:
        user = Path(environment.get("USERPROFILE") or environment.get("HOME") or Path.home())
        roots.extend([user / ".jdks", user / ".m2" / "wrapper" / "dists",
                      user / ".sdkman" / "candidates" / "java", user / ".sdkman" / "candidates" / "maven",
                      user / "scoop" / "apps", user / "dev-env" / "tools", Path("C:/tools")])
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            if environment.get(variable):
                parent = Path(environment[variable])
                roots.extend(parent / name for name in ("Java", "Eclipse Adoptium", "Microsoft", "Amazon Corretto",
                                                       "Zulu", "Semeru", "BellSoft", "Apache", "Maven"))
        if environment.get("LOCALAPPDATA"):
            roots.append(Path(environment["LOCALAPPDATA"]) / "Programs")
    else:
        roots.extend(Path(root) for root in search_roots)
    pending = deque((root, 0) for root in roots)
    visited = set()
    while pending and len(visited) < 800:
        path, depth = pending.popleft()
        try:
            normalized = str(path.resolve()).casefold()
            if normalized in visited or not path.is_dir():
                continue
            visited.add(normalized)
            found = False
            for kind in discovered:
                home = _tool_home(path, kind)
                if home:
                    discovered[kind].add(home)
                    found = True
            if not found and depth < 3:
                with os.scandir(path) as entries:
                    pending.extend((Path(entry.path), depth + 1) for entry in entries
                                   if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."))
        except (OSError, ValueError):
            continue
    return {kind: sorted(paths, key=str.casefold) for kind, paths in discovered.items()}