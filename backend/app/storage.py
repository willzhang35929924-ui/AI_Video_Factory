import re
from pathlib import Path, PurePosixPath

FOLDERS = ("source", "frames", "cutout", "person", "background", "compose", "final", "workflow", "metadata", "logs")


class StoragePaths:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def task_paths(self, project_id: str, task_id: str) -> dict[str, str]:
        for value in (project_id, task_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
                raise ValueError("项目与任务ID不能包含路径分隔符或相对路径")
        base = self.root / "projects" / project_id / "tasks" / task_id
        if not base.resolve().is_relative_to(self.root):
            raise ValueError("存储路径超出项目目录")
        paths = {folder: str(base / folder) for folder in FOLDERS}
        paths["metadata_file"] = str(base / "metadata" / "metadata.json")
        paths["workflow_file"] = str(base / "workflow" / "workflow.json")
        return paths

    def asset_path(self, project_id: str, task_id: str, kind: str, filename: str) -> str:
        if kind not in FOLDERS or PurePosixPath(filename).name != filename or "\\" in filename or filename in ("", ".", ".."):
            raise ValueError("无效的资产目录或文件名")
        folder = Path(self.task_paths(project_id, task_id)[kind])
        path = (folder / filename).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("资产路径超出项目目录")
        return str(path.relative_to(self.root.parent))
