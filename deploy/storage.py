"""One private master document; merge browser edits from writable runtime documents."""

import copy
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class UniqueLoader(yaml.SafeLoader):
    pass


def mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("配置含重复键或非文本键。")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def read(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError
        doc = yaml.load(path.read_text(), Loader=UniqueLoader)
        json.dumps(doc)
        if not isinstance(doc, dict):
            raise ValueError
        return doc
    except (ValueError, TypeError, RecursionError, yaml.YAMLError):
        raise ValueError(f"配置格式无效：{path}") from None


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            yaml.safe_dump(value, output, allow_unicode=True, sort_keys=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("另一个 setup 正在执行，请等待其完成。") from None
        yield


MISSING = object()


def merge(base, ours, theirs, location="config"):
    """Three-way merge. Never silently overwrite a concurrent browser/setup edit."""
    if ours == base:
        return copy.deepcopy(theirs) if theirs is not MISSING else MISSING
    if theirs == base or ours == theirs:
        return copy.deepcopy(ours) if ours is not MISSING else MISSING
    if all(isinstance(v, dict) for v in (base, ours, theirs)):
        result = {}
        for key in base.keys() | ours.keys() | theirs.keys():
            value = merge(
                base.get(key, MISSING),
                ours.get(key, MISSING),
                theirs.get(key, MISSING),
                location + "." + key,
            )
            if value is not MISSING:
                result[key] = value
        return result
    raise ValueError(f"配置同时被修改：{location}。请重新打开向导，核对后再保存。")


def component(role):
    return dict(
        version=1,
        component=role,
        application={},
        environment={},
        docker={},
        assistant={},
    )


def replace_tree(target, value):
    """Preserve field bindings while incorporating concurrent changes."""
    for key in target.keys() - value.keys():
        del target[key]
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(target.get(key), dict):
            replace_tree(target[key], item)
        else:
            target[key] = copy.deepcopy(item)


class Store:
    def __init__(self, path=None):
        selected = path if path is not None else os.environ.get("CONFIG_FILE", ROOT / "config.yaml")
        if not str(selected).strip():
            raise ValueError("CONFIG_FILE 不能为空。")
        self.path = (ROOT / Path(selected).expanduser()).resolve()
        self.root = self.path.parent
        self.var = self.root / "var"
        self.runtime = self.var / "runtime"
        self.baseline_path = self.runtime / "baseline.yaml"
        self.runtime_base = read(self.baseline_path)
        self.original = read(self.path)
        if not self.original and self.runtime_base:
            raise ValueError(
                "统一配置缺失，但已有运行配置。请恢复 config.yaml/.bak；新实例须使用独立目录，避免重新生成原站点密钥。"
            )
        if self.original and (
            self.original.get("version") != 1
            or self.original.get("component") != "everunfold-deploy"
        ):
            raise ValueError("CONFIG_FILE 必须指向统一部署配置，不能指向旧的单端配置。")
        self.doc = copy.deepcopy(self.original) or {
            "version": 1,
            "component": "everunfold-deploy",
            "deployment": {},
            "backend": component("backend"),
            "frontend": component("frontend"),
        }
        for role in ("backend", "frontend"):
            doc = self.doc.get(role)
            if not isinstance(doc, dict) or doc.get("component") != role:
                raise ValueError(f"统一配置缺少 {role} 配置。")
            if doc.get("version") != 1 or any(
                not isinstance(doc.get(key), dict)
                for key in ("application", "environment", "assistant", "docker")
            ):
                raise ValueError(f"{role} 配置分区格式无效。")
            if not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in doc["environment"].items()
            ):
                raise ValueError(f"{role} environment 的值必须为字符串。")
        self.import_runtime()

    def path_for(self, role):
        return self.runtime / role / "config.yaml"

    def import_runtime(self):
        baseline = self.runtime_base
        observed = copy.deepcopy(baseline)
        for role in ("backend", "frontend"):
            current = read(self.path_for(role))
            if current and role in baseline:
                replace_tree(self.doc[role], merge(baseline[role], self.doc[role], current, role))
                observed[role] = current
        self.runtime_base = observed

    def save(self):
        with lock(self.var / "config.lock"):
            latest = read(self.path)
            if self.original:
                replace_tree(self.doc, merge(self.original, self.doc, latest))
            elif latest:
                raise ValueError("配置已由其他进程创建，请重新打开向导。")
            self.import_runtime()
            if self.path.exists() and latest != self.doc:
                write(self.path.with_suffix(self.path.suffix + ".bak"), latest)
            write(self.path, self.doc)
            if self.runtime_base:
                write(self.baseline_path, self.runtime_base)
            self.original = copy.deepcopy(self.doc)

    def render(self):
        self.save()
        # Match the frontend's cross-process switch lock (atomic replace needs directory RW).
        locks = []
        try:
            for role in ("backend", "frontend"):
                target = self.path_for(role)
                target.parent.mkdir(parents=True, exist_ok=True)
                guard = target.with_name(target.name + ".switch.lock")
                try:
                    descriptor = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    raise ValueError(f"{role} 浏览器正在保存配置，请稍后再试。") from None
                locks.append((descriptor, guard))
            self.import_runtime()
            write(self.path, self.doc)
            for role in ("backend", "frontend"):
                write(self.path_for(role), self.doc[role])
            write(self.baseline_path, {r: self.doc[r] for r in ("backend", "frontend")})
            self.runtime_base = {r: copy.deepcopy(self.doc[r]) for r in ("backend", "frontend")}
            self.original = copy.deepcopy(self.doc)
        finally:
            for descriptor, guard in locks:
                os.close(descriptor)
                guard.unlink()
