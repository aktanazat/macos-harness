"""Explicitly recorded navigation, replayed through the existing operations."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import uuid
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol

from ._paths import config_dir
from .errors import ErrorCode, MacOSError
from .ops import Operations, _Deadline
from .receipts import (
    Equals,
    ErrorPayload,
    Gone,
    JSONValue,
    OperationError,
    Outcome,
    Postcondition,
    Present,
    Receipt,
)

if TYPE_CHECKING:
    from .macos import _AppIdentity


_MAX_BYTES = 1024 * 1024


def _invalid(field: str, message: str) -> MacOSError:
    return MacOSError(f"Route {field}: {message}", code=ErrorCode.BAD_REQUEST,
                      details={"field": field})


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(field, "requires a nonempty string")
    return value


def _value(value: object) -> bool | float:
    if type(value) not in (bool, int, float):
        raise _invalid("value", "only boolean and numeric values can be recorded")
    if isinstance(value, float) and not math.isfinite(value):
        raise _invalid("value", "must be finite")
    return value


def _number(value: object, field: str) -> float:
    if type(value) not in (int, float):
        raise _invalid(field, "requires a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise _invalid(field, "must be finite")
    return value


def _keys(value: object, keys: set[str], field: str) -> dict[str, JSONValue]:
    if not isinstance(value, dict) or value.keys() != keys:
        raise _invalid(field, "has missing or unsupported fields")
    return value


def _names(name: str, app: str) -> None:
    if not isinstance(name, str) or re.fullmatch(r"[a-z0-9-]{1,64}", name) is None:
        raise _invalid("name", "use 1–64 lowercase letters, digits or hyphens")
    if not isinstance(app, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", app) is None:
        raise _invalid("app", "requires an exact bundle identifier")


@dataclass(frozen=True, slots=True)
class _Locator:
    role: str
    field: Literal["title", "identifier", "description"]
    value: str

    def query(self) -> dict[str, str]:
        return {"role": self.role, self.field: self.value}

    def to_json(self) -> dict[str, JSONValue]:
        return {"role": self.role, "field": self.field, "value": self.value}


def _locator(role: str, title: str | None, identifier: str | None,
             description: str | None) -> _Locator:
    _text(role, "target.role")
    selected = [(field, value) for field, value in (
        ("title", title), ("identifier", identifier), ("description", description)
    ) if value is not None]
    if len(selected) != 1:
        raise _invalid("target", "requires exactly one title, identifier or description")
    field, value = selected[0]
    return _Locator(role, field, _text(value, "target.value"))


def _load_locator(value: object) -> _Locator:
    data = _keys(value, {"role", "field", "value"}, "target")
    field = data["field"]
    if field not in ("title", "identifier", "description"):
        raise _invalid("target.field", "requires an exact identity field")
    return _Locator(_text(data["role"], "target.role"), field,
                    _text(data["value"], "target.value"))


def _condition(condition: Postcondition, app: str) -> Postcondition:
    if not isinstance(condition, Postcondition):
        raise _invalid("condition", "requires present, gone or equals")
    if (condition.app not in (None, app) or condition.apps is not None or condition.all_apps
            or condition.text is not None or condition.search_key is not None
            or condition.direction != "next" or condition.immediate_descendants_only):
        raise _invalid("condition", "requires an exact target in the route's app")
    if not isinstance(condition.visible_only, bool):
        raise _invalid("condition.visible_only", "requires a boolean")
    _number(condition.interval, "condition.interval")
    if condition.timeout is not None:
        _number(condition.timeout, "condition.timeout")
    _locator(condition.role, condition.title, condition.identifier, condition.description)
    if isinstance(condition, Equals):
        _value(condition.value)
    return replace(condition, app=None)


def _condition_json(condition: Postcondition) -> dict[str, JSONValue]:
    data: dict[str, JSONValue] = {
        "kind": type(condition).__name__.lower(),
        "target": _locator(condition.role, condition.title, condition.identifier,
                           condition.description).to_json(),
        "timeout": condition.timeout, "interval": condition.interval,
        "visible_only": condition.visible_only,
    }
    if isinstance(condition, Equals):
        data.update(attribute=condition.attribute, value=condition.value)
    return data


def _load_condition(value: object, app: str) -> Postcondition:
    if not isinstance(value, dict):
        raise _invalid("condition", "requires an object")
    kind = value.get("kind")
    fields = {"kind", "target", "timeout", "interval", "visible_only"}
    data = _keys(value, fields | ({"attribute", "value"} if kind == "equals" else set()), "condition")
    target = _load_locator(data["target"])
    timeout = None if data["timeout"] is None else _number(data["timeout"], "condition.timeout")
    interval = _number(data["interval"], "condition.interval")
    if not isinstance(data["visible_only"], bool):
        raise _invalid("condition.visible_only", "requires a boolean")
    args = {**target.query(), "timeout": timeout, "interval": interval,
            "visible_only": data["visible_only"]}
    match kind:
        case "present":
            result = Present(**args)
        case "gone":
            result = Gone(**args)
        case "equals":
            result = Equals(**args, attribute=_text(data["attribute"], "condition.attribute"),
                            value=_value(data["value"]))
        case _:
            raise _invalid("condition.kind", "requires present, gone or equals")
    return _condition(result, app)


@dataclass(frozen=True, slots=True)
class _Press:
    target: _Locator
    postcondition: Postcondition
    verb: ClassVar[str] = "press"


@dataclass(frozen=True, slots=True)
class _Set:
    target: _Locator
    value: bool | float
    attribute: str
    postcondition: Postcondition | None
    verb: ClassVar[str] = "set"


@dataclass(frozen=True, slots=True)
class _Toggle:
    target: _Locator
    value: bool
    attribute: str
    postcondition: Postcondition | None
    verb: ClassVar[str] = "toggle"


@dataclass(frozen=True, slots=True)
class _Key:
    key: str
    postcondition: Postcondition
    verb: ClassVar[str] = "key"


_Step = _Press | _Set | _Toggle | _Key


def _step_json(step: _Step) -> dict[str, JSONValue]:
    data: dict[str, JSONValue] = {"verb": step.verb,
        "expect": None if step.postcondition is None else _condition_json(step.postcondition)}
    if isinstance(step, _Key):
        data["key"] = step.key
    else:
        data["target"] = step.target.to_json()
    if isinstance(step, (_Set, _Toggle)):
        data.update(value=step.value, attribute=step.attribute)
    return data


def _load_step(value: object, app: str) -> _Step:
    if not isinstance(value, dict):
        raise _invalid("step", "requires an object")
    verb = value.get("verb")
    match verb:
        case "press" | "key":
            data = _keys(value, {"verb", "expect", "key" if verb == "key" else "target"}, "step")
            condition = _load_condition(data["expect"], app)
            if verb == "key":
                return _Key(_text(data["key"], "key"), condition)
            return _Press(_load_locator(data["target"]), condition)
        case "set" | "toggle":
            data = _keys(value, {"verb", "expect", "target", "value", "attribute"}, "step")
            target = _load_locator(data["target"])
            condition = None if data["expect"] is None else _load_condition(data["expect"], app)
            attribute = _text(data["attribute"], "attribute")
            desired = _value(data["value"])
            if verb == "toggle":
                if not isinstance(desired, bool):
                    raise _invalid("value", "toggle requires a boolean")
                return _Toggle(target, desired, attribute, condition)
            return _Set(target, desired, attribute, condition)
        case _:
            raise _invalid("step.verb", "only press, set, toggle and key are supported")


@dataclass(frozen=True, slots=True)
class _Route:
    name: str
    app: str
    app_name: str
    recorded_at: str
    version: str | None
    build: str | None
    entry: Postcondition
    goal: Postcondition
    steps: tuple[_Step, ...]

    def to_json(self) -> dict[str, JSONValue]:
        return {"schema": 1, "name": self.name, "app": self.app,
                "recorded": {"at": self.recorded_at, "app_name": self.app_name,
                             "version": self.version, "build": self.build},
                "entry": _condition_json(self.entry), "goal": _condition_json(self.goal),
                "steps": [_step_json(step) for step in self.steps]}


def _load_route(value: object, name: str, app: str) -> _Route:
    data = _keys(value, {"schema", "name", "app", "recorded", "entry", "goal", "steps"}, "definition")
    if (not isinstance(data["schema"], int) or isinstance(data["schema"], bool)) or data["schema"] != 1:
        raise _invalid("schema", "unsupported schema version")
    if data["name"] != name or data["app"] != app:
        raise _invalid("definition", "name and app must match the requested file")
    recorded = _keys(data["recorded"], {"at", "app_name", "version", "build"}, "recorded")
    for field in ("version", "build"):
        if recorded[field] is not None and not isinstance(recorded[field], str):
            raise _invalid("recorded." + field, "requires a string or null")
    at = _text(recorded["at"], "recorded.at")
    if datetime.fromisoformat(at).tzinfo is None:
        raise _invalid("recorded.at", "requires a timezone")
    steps = data["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 64:
        raise _invalid("steps", "requires 1–64 steps")
    return _Route(name, app, _text(recorded["app_name"], "recorded.app_name"), at,
                  recorded["version"], recorded["build"],
                  _load_condition(data["entry"], app), _load_condition(data["goal"], app),
                  tuple(_load_step(step, app) for step in steps))


class _RouteHost(Protocol):
    do: Operations
    def _process_identity(self, query: str | int) -> _AppIdentity: ...
    def _same_process(self, expected: _AppIdentity) -> bool: ...
    def _bundle_version(self, path: str | None) -> tuple[str | None, str | None]: ...


@dataclass(frozen=True, slots=True)
class RouteResult:
    """A run stops at the first divergence and retains its failing receipt."""

    name: str
    run_id: str
    status: Literal["done", "already", "planned", "diverged", "invalid"]
    steps_run: tuple[Receipt, ...]
    duration_s: float
    at: str | None = None
    error: ErrorPayload | None = None
    check: Receipt | None = None
    recorded_version: tuple[str | None, str | None] = (None, None)
    current_version: tuple[str | None, str | None] = (None, None)

    def to_json(self) -> dict[str, JSONValue]:
        return {"name": self.name, "run_id": self.run_id, "status": self.status,
                "steps_run": [receipt.to_json() for receipt in self.steps_run],
                "duration_s": self.duration_s, "at": self.at, "error": self.error,
                "check": None if self.check is None else self.check.to_json(),
                "app_version": {"source": "on_disk_bundle",
                    "recorded": {"version": self.recorded_version[0], "build": self.recorded_version[1]},
                    "current": {"version": self.current_version[0], "build": self.current_version[1]}}}


class Routes:
    """``mac.route``: explicit recording, private files and sequential replay."""

    def __init__(self, host: _RouteHost) -> None:
        self._host_ref = weakref.ref(host)

    @property
    def _host(self) -> _RouteHost:
        host = self._host_ref()
        if host is None:
            raise MacOSError("This route surface's MacOS instance is no longer available",
                             code=ErrorCode.UNSUPPORTED_OP)
        return host

    def _path(self, name: str, app: str) -> Path:
        _names(name, app)
        return config_dir() / "routes" / app / (name + ".json")

    def _load(self, name: str, app: str) -> _Route:
        path = self._path(name, app)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="utf-8") as source:
            text = source.read(_MAX_BYTES + 1)
        if len(text.encode("utf-8")) > _MAX_BYTES:
            raise _invalid("file", "exceeds the 1 MiB limit")
        return _load_route(json.loads(text), name, app)

    def _save(self, route: _Route) -> Path:
        path = self._path(route.name, route.app)
        text = json.dumps(route.to_json(), ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if len(text.encode("utf-8")) > _MAX_BYTES:
            raise _invalid("file", "exceeds the 1 MiB limit")
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.parent.parent.chmod(0o700)
        path.parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix="." + route.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as destination:
                destination.write(text)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return path

    def list(self, *, app: str) -> list[dict[str, JSONValue]]:
        """List saved definitions without observing the app or replaying them."""
        directory = self._path("list", app).parent
        result: list[dict[str, JSONValue]] = []
        for path in sorted(directory.glob("*.json")):
            route = self._load(path.stem, app)
            result.append({"name": route.name, "app": route.app, "steps": len(route.steps),
                           "recorded": route.to_json()["recorded"]})
        return result

    def _bind(self, app: str) -> _AppIdentity:
        identity = self._host._process_identity(app)
        if identity.bundle_id != app:
            raise _invalid("app", "the running app's bundle identifier does not match")
        return identity

    def _check_identity(self, identity: _AppIdentity) -> None:
        if not self._host._same_process(identity):
            raise MacOSError("The route's app exited or was replaced; stopped",
                             code=ErrorCode.APP_NOT_FOUND,
                             details={"pid": identity.pid, "reason": "process_changed"})

    def _validate_step(self, step: _Step) -> None:
        ops = self._host.do
        if isinstance(step, _Key):
            ops._host._validate_key(step.key)
        else:
            ops._host.ax._search_key(None, step.target.role)
        ops._validate_postcondition(ops._host, step.postcondition)

    def _expect(self, condition: Postcondition, identity: _AppIdentity,
                deadline: _Deadline, *, probe: bool = False) -> Receipt:
        self._check_identity(identity)
        deadline.check_dispatch()
        timeout = deadline.remaining()
        if condition.timeout is not None:
            timeout = min(timeout, condition.timeout)
        if probe:
            timeout = min(timeout, condition.interval if isinstance(condition, Gone) else 0.0)
        receipt = self._host.do.expect(replace(condition, app=identity.pid, timeout=timeout))
        self._check_identity(identity)
        return receipt

    def _dispatch(self, step: _Step, identity: _AppIdentity, deadline: _Deadline,
                  token: str) -> Receipt:
        self._check_identity(identity)
        deadline.check_dispatch()
        ops = self._host.do
        condition = None if step.postcondition is None else replace(step.postcondition, app=identity.pid)
        args = {"app": identity.pid, "timeout": deadline.remaining(),
                "postcondition": condition}
        if isinstance(step, _Key):
            return ops.key(step.key, **args, once=token)
        if isinstance(step, _Press):
            return ops.press(**step.target.query(), **args, once=token)
        if isinstance(step, _Toggle):
            return ops.toggle(step.value, attribute=step.attribute, **step.target.query(), **args)
        return ops.set(step.value, attribute=step.attribute, **step.target.query(), **args)

    @contextmanager
    def record(self, name: str, *, app: str, entry: Postcondition,
               goal: Postcondition, timeout: float = 30.0) -> Iterator[_Recorder]:
        """Record calls made through the yielded handle; other calls are not recorded."""
        ops = self._host.do
        ops._check_owner()
        self._path(name, app)
        entry, goal = _condition(entry, app), _condition(goal, app)
        ops._validate_postcondition(ops._host, entry)
        ops._validate_postcondition(ops._host, goal)
        deadline = _Deadline(timeout, ops._monotonic)
        with ops._dispatch_lock:
            identity = self._bind(app)
            self._expect(entry, identity, deadline)
            version, build = self._host._bundle_version(identity.path)
            recorder = _Recorder(self, app, identity, deadline)
            try:
                yield recorder
                if recorder._failed or not recorder._steps:
                    raise _invalid("recording", "a failed or empty recording cannot be saved")
                recorder.check = self._expect(goal, identity, deadline)
                route = _Route(name, app, identity.name,
                    datetime.fromtimestamp(ops._wall_clock(), UTC).isoformat(timespec="milliseconds"),
                    version, build, entry, goal, tuple(recorder._steps))
                recorder.path = self._save(route)
            finally:
                recorder._active = False

    def run(self, name: str, *, app: str, timeout: float = 30.0,
            dry_run: bool = False) -> RouteResult:
        """Run once with a fresh id; never retry, resume, activate or roll back."""
        ops = self._host.do
        ops._check_owner()
        deadline = _Deadline(timeout, ops._monotonic)
        run_id = uuid.uuid4().hex
        receipts: list[Receipt] = []
        try:
            route = self._load(name, app)
            for condition in (route.entry, route.goal):
                ops._validate_postcondition(ops._host, condition)
            for step in route.steps:
                self._validate_step(step)
        except (MacOSError, OSError, ValueError, TypeError, OverflowError) as exc:
            error = exc if isinstance(exc, MacOSError) else _invalid("file", str(exc))
            return RouteResult(name, run_id, "invalid", (), deadline.elapsed(), error=error.to_json())
        versions = {"recorded_version": (route.version, route.build), "current_version": (None, None)}
        at = "app"
        check = None
        with ops._dispatch_lock:
            try:
                identity = self._bind(app)
                versions["current_version"] = self._host._bundle_version(identity.path)
                at = "goal"
                try:
                    check = self._expect(route.goal, identity, deadline, probe=True)
                except OperationError as exc:
                    # Only a completed search or comparison can establish that
                    # the goal does not hold. A refused/incomplete read stops.
                    details = exc.details
                    known_unmet = (exc.code == ErrorCode.TIMEOUT
                        and details.get("complete") is not False
                        and ("timeout" in details or "expected" in details))
                    if not known_unmet:
                        raise
                else:
                    return RouteResult(name, run_id, "planned" if dry_run else "already", (), deadline.elapsed(),
                                       check=check, **versions)
                at = "entry"
                check = self._expect(route.entry, identity, deadline)
                if dry_run:
                    first = route.steps[0]
                    if not isinstance(first, _Key):
                        at = "steps[0]"
                        check = self._expect(Present(**first.target.query()), identity, deadline)
                    return RouteResult(name, run_id, "planned", (), deadline.elapsed(),
                                       check=check, **versions)
                for index, step in enumerate(route.steps):
                    at = f"steps[{index}]"
                    try:
                        receipt = self._dispatch(step, identity, deadline,
                            f"route:{app}:{name}:{run_id}:{index}")
                    except OperationError as exc:
                        receipts.append(exc.receipt)
                        raise
                    receipts.append(receipt)
                    if receipt.outcome not in (Outcome.DONE, Outcome.ALREADY) or (
                            step.postcondition is not None and not receipt.verified):
                        raise _invalid(at, "operation did not confirm its recorded effect")
                at = "goal"
                check = self._expect(route.goal, identity, deadline)
                return RouteResult(name, run_id, "done", tuple(receipts), deadline.elapsed(),
                                   check=check, **versions)
            except MacOSError as exc:
                if isinstance(exc, OperationError) and exc.receipt.op == "expect":
                    check = exc.receipt
                return RouteResult(name, run_id, "diverged", tuple(receipts), deadline.elapsed(),
                                   at=at, error=exc.to_json(), check=check, **versions)


class _Recorder:
    def __init__(self, routes: Routes, app: str, identity: _AppIdentity, deadline: _Deadline) -> None:
        self._routes = routes
        self._app = app
        self._identity = identity
        self._deadline = deadline
        self._run_id = uuid.uuid4().hex
        self._steps: list[_Step] = []
        self._receipts: list[Receipt] = []
        self._failed = False
        self._active = True
        self._thread_id = threading.get_ident()
        self.path: Path | None = None
        self.check: Receipt | None = None

    @property
    def receipts(self) -> tuple[Receipt, ...]:
        return tuple(self._receipts)

    def _begin(self) -> None:
        self._routes._host.do._check_owner()
        if threading.get_ident() != self._thread_id:
            raise _invalid("recording", "must stay on its with-block thread")
        if not self._active or self._failed:
            raise _invalid("recording", "is closed or has failed")
        self._failed = True
        if len(self._steps) >= 64:
            raise _invalid("steps", "cannot record more than 64 steps")

    def _apply(self, step: _Step) -> Receipt:
        self._routes._validate_step(step)
        try:
            receipt = self._routes._dispatch(step, self._identity, self._deadline,
                f"record:{self._app}:{self._run_id}:{len(self._steps)}")
        except OperationError as exc:
            self._receipts.append(exc.receipt)
            raise
        self._receipts.append(receipt)
        if receipt.outcome not in (Outcome.DONE, Outcome.ALREADY) or (
                step.postcondition is not None and not receipt.verified):
            raise _invalid("recording", "operation did not confirm its effect")
        self._steps.append(step)
        self._failed = False
        return receipt

    def press(self, *, role: str, postcondition: Postcondition, title: str | None = None,
              identifier: str | None = None, description: str | None = None) -> Receipt:
        self._begin()
        return self._apply(_Press(_locator(role, title, identifier, description),
                                  _condition(postcondition, self._app)))

    def set(self, value: bool | float, *, role: str, title: str | None = None,
            identifier: str | None = None, description: str | None = None,
            attribute: str = "AXValue", postcondition: Postcondition | None = None) -> Receipt:
        self._begin()
        return self._apply(_Set(_locator(role, title, identifier, description), _value(value),
            _text(attribute, "attribute"),
            None if postcondition is None else _condition(postcondition, self._app)))

    def toggle(self, desired: bool, *, role: str, title: str | None = None,
               identifier: str | None = None, description: str | None = None,
               attribute: str = "AXValue", postcondition: Postcondition | None = None) -> Receipt:
        self._begin()
        if not isinstance(desired, bool):
            raise _invalid("value", "toggle requires a boolean")
        return self._apply(_Toggle(_locator(role, title, identifier, description), desired,
            _text(attribute, "attribute"),
            None if postcondition is None else _condition(postcondition, self._app)))

    def key(self, key: str, *, postcondition: Postcondition) -> Receipt:
        self._begin()
        return self._apply(_Key(_text(key, "key"), _condition(postcondition, self._app)))
