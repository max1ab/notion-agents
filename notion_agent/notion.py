from __future__ import annotations

import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


class NotionError(RuntimeError):
    pass


class CommentWriteUncertain(NotionError):
    """An attempted comment cannot yet be reconciled; never append blindly."""


def rich(text):
    return [{"type": "text", "text": {"content": text[i:i + 1800]}} for i in range(0, len(text), 1800)]


def text_of(items):
    return "".join(x.get("plain_text", x.get("text", {}).get("content", "")) for x in items)


def value(page, name):
    p = page.get("properties", {}).get(name, {})
    kind = p.get("type")
    if kind in ("status", "select"):
        return (p.get(kind) or {}).get("name", "")
    if kind in ("title", "rich_text"):
        return text_of(p.get(kind, []))
    return p.get(kind, "") if kind else ""


def project_key(page):
    prop = page.get("properties", {}).get("项目", {})
    if prop.get("type") == "relation":
        links = prop.get("relation", [])
        if prop.get("has_more") or len(links) != 1:
            return ""
        return links[0]["id"]
    return value(page, "项目")


class Notion:
    def __init__(self, token, data_source_id):
        self.token = token
        self.source = data_source_id
        self.last_request = 0.0

    def request(self, method, path, body=None):
        """Retry reads/rate limits only. Ambiguous mutations reconcile on next scan."""
        for attempt in range(5):
            time.sleep(max(0, 0.35 - (time.monotonic() - self.last_request)))
            req = urllib.request.Request("https://api.notion.com/v1/" + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": "Bearer " + self.token,
                         "Notion-Version": "2025-09-03", "Content-Type": "application/json"}, method=method)
            self.last_request = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 429:
                    time.sleep(min(float(error.headers.get("Retry-After", 2 ** attempt)), 60))
                    continue
                if method == "GET" and error.code >= 500 and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise NotionError(f"Notion {method} {path}: HTTP {error.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                raise NotionError(f"Notion {method} {path}: 网络错误或响应不确定") from None
        raise NotionError("Notion 限流，稍后重试")

    def source_schema(self):
        return self.request("GET", "data_sources/" + self.source)

    def check_schema(self):
        schema = self.source_schema()["properties"]
        self.project_property_type = schema.get("项目", {}).get("type")
        if self.project_property_type not in ("select", "relation"):
            raise ValueError("项目字段必须为关联或旧版 select")
        self.project_relation_target = schema.get("项目", {}).get("relation", {}).get("data_source_id")
        expected = {"任务": "title", "状态": "status", "执行 Agent": "select",
                    "类型": "select", "优先级": "select", "最近运行 ID": "rich_text", "运行摘要": "rich_text"}
        if self.project_property_type == "select":
            expected["执行机器"] = "select"
        errors = [f"{key} 需要 {kind}" for key, kind in expected.items() if schema.get(key, {}).get("type") != kind]
        options = {x["name"] for x in schema.get("状态", {}).get("status", {}).get("options", [])}
        errors += [f"缺少状态：{name}" for name in ("待办", "执行中", "待我验收", "完成", "需处理") if name not in options]
        if errors:
            raise ValueError("；".join(errors))
        return schema

    def register(self, machine_id, project_keys):
        """Incrementally add local options; keep all existing option IDs."""
        schema = self.source_schema()["properties"]
        if schema["项目"]["type"] == "relation":
            return
        changes = {}
        for key, names in (("执行机器", [machine_id]), ("项目", project_keys)):
            options = schema[key]["select"]["options"]
            old = {o["name"] for o in options}
            added = [{"name": n} for n in names if n not in old]
            if added:
                changes[key] = {"select": {"options": [dict(id=o["id"], name=o["name"], color=o["color"]) for o in options] + added}}
        if changes:
            self.request("PATCH", "data_sources/" + self.source, {"properties": changes})

    def query(self, machine_id, project_ids=None):
        if project_ids is not None and not project_ids:
            return []
        groups = [None] if project_ids is None else [project_ids[i:i + 90] for i in range(0, len(project_ids), 90)]
        rows = []
        for group in groups:
            routing = ({"property": "执行机器", "select": {"equals": machine_id}} if group is None else
                       {"or": [{"property": "项目", "relation": {"contains": id}} for id in group]})
            body = {"filter": {"and": [{"property": "状态", "status": {"equals": "待办"}}, routing]}, "page_size": 100}
            while True:
                result = self.request("POST", f"data_sources/{self.source}/query", body)
                rows.extend(result["results"])
                if not result.get("has_more"):
                    break
                body["start_cursor"] = result["next_cursor"]
        rows = list({p["id"]: p for p in rows}.values())
        return sorted(rows, key=lambda p: ({"高": 0, "中": 1, "低": 2}.get(value(p, "优先级"), 3), p.get("created_time", "")))

    def projects(self, source_id, machine_id, by_id=False):
        schema = self.request("GET", "data_sources/" + source_id)["properties"]
        for name, kind in (("项目", "title"), ("执行机器", "rich_text"), ("本机目录", "rich_text")):
            if schema.get(name, {}).get("type") != kind:
                raise ValueError(f"项目表字段 {name} 必须为 {kind}")
        body = {"filter": {"property": "执行机器", "rich_text": {"equals": machine_id}}, "page_size": 100}
        projects = {}
        while True:
            result = self.request("POST", f"data_sources/{source_id}/query", body)
            for row in result["results"]:
                if row.get("archived") or row.get("in_trash"):
                    continue
                if value(row, "执行机器") != machine_id:
                    continue
                name = value(row, "项目").strip()
                if not name:
                    continue
                path = value(row, "本机目录").strip()
                entry = {"path": path, "notion_page_id": row["id"],
                         "notion_last_edited_time": row.get("last_edited_time"),
                         "name": name, "machine_id": value(row, "执行机器")}
                if not path or not Path(path).expanduser().is_absolute():
                    entry["error"] = "Notion 项目配置需要填写本机绝对目录"
                if not by_id and name in projects:
                    entry["error"] = "Notion 中此项目在本机有多条配置，请保留一条"
                projects[row["id"] if by_id else name] = entry
            if not result.get("has_more"):
                return projects
            body["start_cursor"] = result["next_cursor"]

    def page(self, page_id):
        return self.request("GET", "pages/" + page_id)

    def children(self, block_id):
        cursor = None
        while True:
            query = "?page_size=100" + ("&start_cursor=" + urllib.parse.quote(cursor) if cursor else "")
            result = self.request("GET", f"blocks/{block_id}/children" + query)
            yield from result["results"]
            if not result.get("has_more"):
                break
            cursor = result["next_cursor"]

    def body(self, page_id):
        supported = {"paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item",
                     "to_do", "toggle", "quote", "callout", "code", "table_row", "table", "column", "column_list", "divider", "bookmark", "link_preview", "link_to_page"}
        budget = [2000]
        def walk(block_id, depth=0):
            if depth > 12:
                raise ValueError("正文嵌套超过 12 层")
            lines = []
            for block in self.children(block_id):
                budget[0] -= 1
                if budget[0] < 0:
                    raise ValueError("正文超过 2000 块")
                kind = block["type"]
                data = block.get(kind, {})
                text = text_of(data.get("rich_text", []))
                if kind == "toggle" and text.startswith("Agent result · "):
                    continue
                if kind not in supported:
                    raise ValueError(f"正文含暂不支持的 {kind}；请改为文字或明确的链接")
                if kind == "table_row":
                    text = " | ".join(text_of(cell) for cell in data.get("cells", []))
                if kind in ("bookmark", "link_preview"):
                    text = data.get("url", "")
                if kind == "link_to_page":
                    text = "https://www.notion.so/" + data.get(data.get("type", ""), "")
                # Preserve explicit hyperlinks in task input; do not download them here.
                links = [r.get("href") or r.get("text", {}).get("link", {}).get("url") for r in data.get("rich_text", []) if r.get("href") or r.get("text", {}).get("link")]
                lines.append("  " * depth + text + (" " + " ".join(links) if links else ""))
                if block.get("has_children"):
                    lines.extend(walk(block["id"], depth + 1))
            return lines
        return "\n".join(walk(page_id)).strip()

    def update(self, page_id, *, status=None, run_id=None, summary=None):
        props = {}
        if status is not None:
            props["状态"] = {"status": {"name": status}}
        if run_id is not None:
            props["最近运行 ID"] = {"rich_text": rich(run_id)}
        if summary is not None:
            props["运行摘要"] = {"rich_text": rich(summary[:1800])}
        return self.request("PATCH", "pages/" + page_id, {"properties": props})

    def result_chunk(self, page_id, marker, content):
        matches = [b for b in self.children(page_id) if b["type"] == "toggle" and text_of(b["toggle"].get("rich_text", [])) == marker]
        if len(matches) > 1:
            raise NotionError("结果标记重复，需要人工对账")
        if matches:
            return matches[0]["id"]
        paragraphs = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(content[i:i + 1800])}}
                      for i in range(0, len(content), 1800)]
        block = {"object": "block", "type": "toggle", "toggle": {"rich_text": rich(marker), "children": paragraphs}}
        result = self.request("PATCH", f"blocks/{page_id}/children", {"children": [block]})
        return result["results"][0]["id"]

    def comment(self, page_id, marker, message):
        cursor = None
        while True:
            path = "comments?block_id=" + page_id + ("&start_cursor=" + cursor if cursor else "")
            result = self.request("GET", path)
            if any(marker in text_of(c.get("rich_text", [])) for c in result["results"]):
                return
            if not result.get("has_more"):
                break
            cursor = result["next_cursor"]
        self.request("POST", "comments", {"parent": {"page_id": page_id}, "rich_text": rich(marker + "\n" + message)})

    def post_comment(self, page_id, message, operation, persist, discussion_id=None, display_name=None):
        """Keep idempotency receipts locally, without adding visible IDs to comments."""
        if operation.get("comment_id"):
            return operation["comment_id"], operation["remote_discussion_id"]
        comments = []
        cursor = None
        while True:
            path = "comments?block_id=" + page_id
            if cursor:
                path += "&start_cursor=" + urllib.parse.quote(cursor)
            result = self.request("GET", path)
            comments.extend(result["results"])
            if not result.get("has_more"):
                break
            cursor = result["next_cursor"]
        if "baseline_ids" not in operation:
            actor = self.request("GET", "users/me")["id"]
            operation.update(baseline_ids=[c["id"] for c in comments], message=message,
                             discussion_id=discussion_id, actor_id=actor, display_name=display_name)
            persist()
        elif (message != operation["message"] or discussion_id != operation["discussion_id"]
              or display_name != operation.get("display_name")):
            raise CommentWriteUncertain("待回写评论内容或署名发生变化，请核对本地运行记录")
        if operation.get("attempted"):
            matches = [c for c in comments if c["id"] not in operation["baseline_ids"]
                       and text_of(c.get("rich_text", [])) == message
                       and c.get("created_by", {}).get("id") == operation["actor_id"]
                       and (not display_name or c.get("display_name", {}).get("resolved_name") == display_name)
                       and (not discussion_id or c.get("discussion_id") == discussion_id)]
            if len(matches) != 1:
                raise CommentWriteUncertain("评论写入结果未能唯一确认；可能已解决或删除，请人工核对，暂不重复发送")
            result = matches[0]
        else:
            operation["attempted"] = True
            persist()
            target = {"discussion_id": discussion_id} if discussion_id else {"parent": {"page_id": page_id}}
            payload = {**target, "rich_text": rich(message)}
            if display_name:
                payload["display_name"] = {"type": "custom", "custom": {"name": display_name}}
            result = self.request("POST", "comments", payload)
        operation.update(comment_id=result["id"], remote_discussion_id=result["discussion_id"])
        persist()
        return result["id"], result["discussion_id"]
