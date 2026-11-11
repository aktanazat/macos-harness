"""An app's AppleScript dictionary, read from its bundle without launching it."""

from __future__ import annotations

import plistlib
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

from .errors import ErrorCode, MacOSError
from .receipts import JSONValue

# Safari's dictionary names XInclude by its 2003 draft namespace.
_XINCLUDE = frozenset(
    f"{{http://www.w3.org/{year}/XInclude}}include" for year in ("2001", "2003")
)
# The two XPointers app dictionaries use to pull in CocoaStandard.sdef:
# its suites whole, or every node of them but some named commands.
_ALL_SUITES = "xpointer(/dictionary/suite)"
_SUITE_NODES = re.compile(r"xpointer\(/dictionary/suite/node\(\)\[not\(self::command and (.*)\)\]\)")
_EXCLUDED_NAME = re.compile(r"@name\s*=\s*'([^']*)'")


class _UnreadableInclude(Exception):
    """An XInclude this reader cannot follow, so `/usr/bin/sdef` reads the app."""


def _definition_file(bundle: Path) -> Path | None:
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as source:
            info = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    name = info.get("OSAScriptingDefinition") if isinstance(info, dict) else None
    if not isinstance(name, str) or not name:
        return None
    # Messages names its Messages.sdef without the extension.
    return bundle / "Contents" / "Resources" / (name if Path(name).suffix else f"{name}.sdef")


def _sdef_tool(bundle: Path) -> ET.Element:
    result = subprocess.run(
        ["/usr/bin/sdef", str(bundle)], capture_output=True, check=False
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise MacOSError(
            f"{bundle.name} has no scripting dictionary, so mac.script can only drive it "
            'through "System Events" UI scripting',
            code=ErrorCode.UNSUPPORTED_OP,
            details={"path": str(bundle), "sdef": result.stderr.decode(errors="replace").strip()},
        )
    return ET.fromstring(result.stdout)


def _included(include: ET.Element, base: Path) -> list[ET.Element]:
    href = include.get("href", "")
    parsed = urlparse(href)
    if parsed.scheme not in ("", "file"):
        raise _UnreadableInclude(href)
    path = Path(unquote(parsed.path))
    root = ET.parse(path if path.is_absolute() else base / path).getroot()
    pointer = include.get("xpointer")
    if pointer is None:
        return [root]
    if pointer == _ALL_SUITES:
        return root.findall("suite")
    match = _SUITE_NODES.fullmatch(pointer)
    if match is None:
        raise _UnreadableInclude(pointer)
    left_out = set(_EXCLUDED_NAME.findall(match.group(1)))
    return [
        node for suite in root.findall("suite") for node in suite
        if not (node.tag == "command" and node.get("name") in left_out)
    ]


def _shown(element: ET.Element) -> bool:
    return element.get("hidden") not in ("yes", "true")


def _type(element: ET.Element) -> str | None:
    kinds = [
        f"list of {kind}" if node.get("list") == "yes" else kind
        for node in (element, *element.findall("type"))
        if (kind := node.get("type"))
    ]
    return " | ".join(kinds) or None


def _described(entry: dict[str, JSONValue], element: ET.Element) -> dict[str, JSONValue]:
    description = element.get("description")
    if description:
        entry["description"] = description
    return entry


def _parameter(element: ET.Element) -> dict[str, JSONValue]:
    entry: dict[str, JSONValue] = {"name": element.get("name", ""), "type": _type(element)}
    if element.get("optional") == "yes":
        entry["optional"] = True
    return _described(entry, element)


def _command(element: ET.Element) -> dict[str, JSONValue]:
    entry = _described({"name": element.get("name", "")}, element)
    direct = element.find("direct-parameter")
    if direct is not None:
        entry["direct"] = {key: value for key, value in _parameter(direct).items() if key != "name"}
    entry["parameters"] = [_parameter(node) for node in element.findall("parameter") if _shown(node)]
    result = element.find("result")
    if result is not None:
        entry["result"] = _type(result)
    return entry


_MEMBERS = ("properties", "elements", "responds_to")
_UNREADABLE = (_UnreadableInclude, OSError, ET.ParseError)


class _Reader:
    def __init__(self) -> None:
        self.commands: dict[str, dict[str, JSONValue]] = {}
        self.classes: dict[str, dict[str, JSONValue]] = {}
        self.members: dict[str, dict[str, list[JSONValue]]] = {}
        self.enumerations: dict[str, list[JSONValue]] = {}

    def read(self, element: ET.Element, base: Path) -> _Reader:
        if element.tag == "dictionary" or (element.tag == "suite" and _shown(element)):
            for child in element:
                self.read(child, base)
        elif element.tag in _XINCLUDE:
            for child in _included(element, base):
                self.read(child, base)
        elif not _shown(element):
            return self
        elif element.tag == "command":
            self.commands[element.get("name", "")] = _command(element)
        elif element.tag in ("class", "class-extension"):
            self._class(element)
        elif element.tag == "enumeration":
            self.enumerations[element.get("name", "")] = [
                node.get("name", "") for node in element.findall("enumerator") if _shown(node)
            ]
        return self

    def _class(self, element: ET.Element) -> None:
        name = element.get("name") or element.get("extends") or ""
        entry = _described(self.classes.setdefault(name, {"name": name}), element)
        for key in ("plural", "inherits"):
            if element.get(key):
                entry[key] = element.get(key)
        members = self.members.setdefault(name, {key: [] for key in _MEMBERS})
        members["properties"] += [
            {"name": node.get("name", ""), "type": _type(node), "access": node.get("access", "rw")}
            for node in element.findall("property") if _shown(node)
        ]
        members["elements"] += [
            node.get("type", "") for node in element.findall("element") if _shown(node)
        ]
        members["responds_to"] += [
            node.get("command") or node.get("name") or ""
            for node in element.findall("responds-to") if _shown(node)
        ]

    def summary(self, bundle: Path, source: str) -> dict[str, JSONValue]:
        return {
            "path": str(bundle),
            "source": source,
            "commands": list(self.commands.values()),
            "classes": [
                {**entry, **{key: values for key, values in self.members[name].items() if values}}
                for name, entry in self.classes.items()
            ],
            "enumerations": [
                {"name": name, "values": values} for name, values in self.enumerations.items()
            ],
        }


def _from_definition(bundle: Path) -> dict[str, JSONValue] | None:
    definition = _definition_file(bundle)
    if definition is None:
        return None
    try:
        reader = _Reader().read(ET.parse(definition).getroot(), definition.parent)
    except _UNREADABLE:
        return None
    return reader.summary(bundle, definition.name)


def dictionary(bundle: Path) -> dict[str, JSONValue]:
    """The commands, classes, and enumerations ``bundle``'s scripting
    dictionary declares, its hidden ones left out: read from the file its
    Info.plist names, or, for a dictionary in another form or one with an
    XInclude this reader does not follow, from `/usr/bin/sdef`."""
    found = _from_definition(bundle)
    if found is not None:
        return found
    try:
        reader = _Reader().read(_sdef_tool(bundle), bundle / "Contents" / "Resources")
    except _UNREADABLE as exc:
        raise MacOSError(
            f"{bundle.name}'s scripting dictionary did not read: {exc}",
            code=ErrorCode.UNSUPPORTED_OP,
            details={"path": str(bundle)},
        ) from exc
    return reader.summary(bundle, "/usr/bin/sdef")
