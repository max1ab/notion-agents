from pathlib import Path


def validate(project, kind):
    if project.get("error"):
        raise ValueError(project["error"])
    path = Path(project["path"]).expanduser()
    if not path.is_absolute():
        raise ValueError("项目目录必须是绝对路径")
    if not path.is_dir():
        raise ValueError(f"项目目录不存在：{path}")


def prepare(project, kind, run_dir, run_id):
    validate(project, kind)
    return {"workspace": str(Path(project["path"]).expanduser().resolve())}


def overlaps(first: Path, second: Path):
    if first.is_relative_to(second) or second.is_relative_to(first):
        return True
    # Different spellings can refer to the same directory on case-insensitive volumes.
    for directory, other in ((first, second), (second, first)):
        for ancestor in (other, *other.parents):
            try:
                if directory.samefile(ancestor):
                    return True
            except OSError:
                continue
    return False
