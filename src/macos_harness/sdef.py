"""An app's AppleScript dictionary, read from its bundle without launching it."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import objc
from Foundation import NSURL, NSBundle

from .errors import ErrorCode, MacOSError
from .receipts import JSONValue

# Measured: mode 0 returns an .sdef with its XIncludes, mode 2 with them
# resolved; both convert a .scriptSuite dictionary, as TextEdit's is.
_RESOLVE_INCLUDES = 2
_OPEN_SCRIPTING: dict[str, object] = {}


def _scripting_definition(bundle: Path) -> tuple[int, object]:
    if not _OPEN_SCRIPTING:
        objc.loadBundleFunctions(
            NSBundle.bundleWithPath_(
                "/System/Library/Frameworks/Carbon.framework/Frameworks/OpenScripting.framework"
            ),
            _OPEN_SCRIPTING,
            [("OSACopyScriptingDefinitionFromURL", b"i@io^@")],
        )
    copy = _OPEN_SCRIPTING["OSACopyScriptingDefinitionFromURL"]
    assert callable(copy)
    return copy(NSURL.fileURLWithPath_(str(bundle)), _RESOLVE_INCLUDES, None)


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


class _Reader:
    def __init__(self) -> None:
        self.commands: dict[str, dict[str, JSONValue]] = {}
        self.classes: dict[str, dict[str, JSONValue]] = {}
        self.members: dict[str, dict[str, list[JSONValue]]] = {}
        self.enumerations: dict[str, list[JSONValue]] = {}

    def read(self, element: ET.Element) -> _Reader:
        if element.tag == "dictionary" or (element.tag == "suite" and _shown(element)):
            for child in element:
                self.read(child)
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

    def summary(self, bundle: Path) -> dict[str, JSONValue]:
        return {
            "path": str(bundle),
            "commands": list(self.commands.values()),
            "classes": [
                {**entry, **{key: values for key, values in self.members[name].items() if values}}
                for name, entry in self.classes.items()
            ],
            "enumerations": [
                {"name": name, "values": values} for name, values in self.enumerations.items()
            ],
        }


def dictionary(bundle: Path) -> dict[str, JSONValue]:
    """The commands, classes, and enumerations ``bundle``'s scripting
    dictionary declares, its hidden ones left out, as OpenScripting reads
    it for Script Editor: the .sdef its Info.plist names, its XIncludes
    resolved, or one converted from older .scriptSuite files."""
    status, data = _scripting_definition(bundle)
    if status != 0 or data is None:
        raise MacOSError(
            f"{bundle.name} has no scripting dictionary, so mac.script can only drive it "
            'through "System Events" UI scripting',
            code=ErrorCode.UNSUPPORTED_OP,
            details={"path": str(bundle), "status": status},
        )
    try:
        root = ET.fromstring(bytes(data))
    except ET.ParseError as exc:
        raise MacOSError(
            f"{bundle.name}'s scripting dictionary did not read: {exc}",
            code=ErrorCode.UNSUPPORTED_OP,
            details={"path": str(bundle)},
        ) from exc
    return _Reader().read(root).summary(bundle)
