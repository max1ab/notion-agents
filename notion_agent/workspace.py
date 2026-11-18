from pathlib import Path
import subprocess


def git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError("Git 检查或操作失败：" + result.stderr.strip()[:500])
    return result.stdout.strip()


def validate(project, kind):
    path = Path(project["path"])
    if not path.is_dir():
        raise ValueError(f"项目目录不存在：{path}")
    if kind == "代码":
        if not project.get("base"):
            raise ValueError("代码项目必须配置基线 base")
        git(path, "rev-parse", "--show-toplevel")
        git(path, "rev-parse", "--verify", project["base"] + "^{commit}")
        if git(path, "status", "--porcelain"):
            raise ValueError("项目有未提交改动，无法确认任务是否依赖它们；请先处理工作区")


def prepare(project, kind, run_dir, run_id):
    validate(project, kind)
    workspace = run_dir / "workspace"
    metadata = {"workspace": str(workspace)}
    if kind == "代码":
        metadata["baseline"] = git(project["path"], "rev-parse", project["base"] + "^{commit}")
        metadata["branch"] = "notion/" + run_id
        git(project["path"], "worktree", "add", "-b", metadata["branch"], str(workspace), metadata["baseline"])
    else:
        workspace.mkdir(mode=0o700)
        # The project is reference context, not an unrestricted shared output directory.
        (workspace / "PROJECT_CONTEXT.md").write_text(
            f"Reference project directory: {project['path']}\nWrite task output in this workspace.\n")
    return metadata
