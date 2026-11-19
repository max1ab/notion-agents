from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


class NotionError(RuntimeError):
    pass


class NotionRequestRejected(NotionError):
    """Notion explicitly rejected the request before applying it."""


class CommentWriteUncertain(NotionError):
    """An attempted comment cannot yet be reconciled; never append blindly."""


class ExecutionWriteUncertain(NotionError):
    """A record create may have succeeded; reconcile before attempting another."""


TIMING_PROPERTIES = {"最近耗时（秒）": "latest_seconds", "累计耗时（秒）": "total_seconds", "执行次数": "execution_count"}


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
                status = error.code
                retry_after = error.headers.get("Retry-After") if error.headers else None
                error.close()
                if status == 429:
                    time.sleep(min(float(retry_after or 2 ** attempt), 60))
                    continue
                if method == "GET" and status >= 500 and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                failure = NotionRequestRejected if status in (400, 401, 403, 404, 413, 422) else NotionError
                raise failure(f"Notion {method} {path}: HTTP {status}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                raise NotionError(f"Notion {method} {path}: 网络错误或响应不确定") from None
        raise NotionRequestRejected("Notion 限流，稍后重试")

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
        errors += [f"缺少状态：{name}" for name in ("草稿", "待办", "执行中", "待我验收", "完成", "需处理") if name not in options]
        self.timing_enabled = all(schema.get(name, {}).get("type") == "number" for name in TIMING_PROPERTIES)
        if any(name in schema for name in TIMING_PROPERTIES) and not self.timing_enabled:
            errors.append("耗时统计需要最近耗时（秒）、累计耗时（秒）、执行次数三个 number 字段")
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

    def query(self, machine_id, project_ids=None, status="待办"):
        if project_ids is not None and not project_ids:
            return []
        groups = [None] if project_ids is None else [project_ids[i:i + 90] for i in range(0, len(project_ids), 90)]
        rows = []
        for group in groups:
            routing = ({"property": "执行机器", "select": {"equals": machine_id}} if group is None else
                       {"or": [{"property": "项目", "relation": {"contains": id}} for id in group]})
            filters = [routing]
            if status is not None:
                filters.insert(0, {"property": "状态", "status": {"equals": status}})
            body = {"filter": {"and": filters}, "page_size": 100}
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

    def update_timings(self, page_id, stats):
        if getattr(self, "timing_enabled", False) and stats:
            self.request("PATCH", "pages/" + page_id, {"properties": {
                name: {"number": stats[key]} for name, key in TIMING_PROPERTIES.items()}})

    def check_execution_schema(self, source_id):
        schema = self.request("GET", "data_sources/" + source_id)["properties"]
        expected = {"执行": "title", "任务": "relation", "Agent": "select", "执行时间": "date",
                    "耗时（秒）": "number", "结果": "select", "触发方式": "select",
                    "机器": "rich_text", "运行 ID": "rich_text",
                    "开始时间（原始）": "rich_text", "结束时间（原始）": "rich_text"}
        errors = [name for name, kind in expected.items() if schema.get(name, {}).get("type") != kind]
        if schema.get("任务", {}).get("relation", {}).get("data_source_id") != self.source:
            errors.append("任务关联目标")
        options = {o["name"] for o in schema.get("结果", {}).get("select", {}).get("options", [])}
        if not {"执行中", "待我验收", "需处理"}.issubset(options):
            errors.append("结果选项")
        if errors:
            raise ValueError("执行记录表字段不匹配：" + ", ".join(errors))
        self._execution_source_checked = source_id

    def sync_execution(self, source_id, run, persist):
        if getattr(self, "_execution_source_checked", None) != source_id:
            self.check_execution_schema(source_id)
        operation = run.setdefault("execution_record", {"source_id": source_id})
        if operation["source_id"] != source_id:
            raise ExecutionWriteUncertain("执行记录的数据源发生变化，请核对本地运行记录")
        dates = {"start": run["started_at"]}
        seconds = None
        if run.get("ended_at"):
            dates["end"] = run["ended_at"]
            seconds = max(0, round((datetime.fromisoformat(run["ended_at"].replace("Z", "+00:00"))
                                   - datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))).total_seconds(), 3))
        properties = {"执行": {"title": rich(run["snapshot"]["title"] + " · " + run["agent"])},
                      "任务": {"relation": [{"id": run["page_id"]}]},
                      "Agent": {"select": {"name": run["agent"]}}, "执行时间": {"date": dates},
                      "开始时间（原始）": {"rich_text": rich(run["started_at"])},
                      "结束时间（原始）": {"rich_text": rich(run.get("ended_at", ""))},
                      "耗时（秒）": {"number": seconds},
                      "结果": {"select": {"name": run.get("target_status", "执行中")}},
                      "触发方式": {"select": {"name": "评论续聊" if run.get("trigger") else "任务派单"}},
                      "机器": {"rich_text": rich(run["machine"])}, "运行 ID": {"rich_text": rich(run["id"])}}
        if not operation.get("page_id"):
            result = self.request("POST", "data_sources/" + source_id + "/query",
                                  {"filter": {"property": "运行 ID", "rich_text": {"equals": run["id"]}}, "page_size": 2})
            matches = result["results"]
            if len(matches) > 1 or result.get("has_more"):
                raise ExecutionWriteUncertain("同一运行 ID 对应多条执行记录，请人工核对")
            if matches:
                links = matches[0].get("properties", {}).get("任务", {}).get("relation", [])
                if len(links) != 1 or links[0]["id"].replace("-", "") != run["page_id"].replace("-", ""):
                    raise ExecutionWriteUncertain("执行记录关联了其他任务，请人工核对")
                operation["page_id"] = matches[0]["id"]
                persist()
            elif operation.get("attempted"):
                raise ExecutionWriteUncertain("执行记录创建结果未确认，可能已删除或尚不可见；暂不重复创建")
            else:
                operation["attempted"] = True
                persist()
                try:
                    result = self.request("POST", "pages", {"parent": {"type": "data_source_id", "data_source_id": source_id},
                                                            "properties": properties})
                except NotionRequestRejected:
                    # There was no remote mutation. The next retry can create after checking for a row.
                    operation.pop("attempted")
                    persist()
                    raise
                operation["page_id"] = result["id"]
                persist()
                return result["id"]
        self.request("PATCH", "pages/" + operation["page_id"], {"properties": properties})
        return operation["page_id"]

    def list_comments(self, page_id):
        comments = []
        cursor = None
        while True:
            path = "comments?block_id=" + page_id + "&page_size=100"
            if cursor:
                path += "&start_cursor=" + urllib.parse.quote(cursor)
            result = self.request("GET", path)
            comments.extend(result["results"])
            if not result.get("has_more"):
                return sorted(comments, key=lambda c: (c.get("created_time", ""), c["id"]))
            cursor = result["next_cursor"]

    def comment_author_is_bot(self, comment):
        actor = comment.get("created_by", {})
        if actor.get("type"):
            return actor["type"] == "bot"
        if not hasattr(self, "_comment_authors"):
            self._comment_authors = {}
        actor_id = actor["id"]
        if actor_id not in self._comment_authors:
            self._comment_authors[actor_id] = self.request("GET", "users/" + actor_id)["type"]
        return self._comment_authors[actor_id] == "bot"

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
            me = self.request("GET", "users/me")
            owner = me.get("bot", {}).get("owner", {})
            actors = [me["id"]]
            if owner.get("type") == "user" and owner.get("user", {}).get("id"):
                actors.append(owner["user"]["id"])
            operation.update(baseline_ids=[c["id"] for c in comments], message=message,
                             discussion_id=discussion_id, actor_id=me["id"], actor_ids=actors, display_name=display_name)
            persist()
        elif (message != operation["message"] or discussion_id != operation["discussion_id"]
              or display_name != operation.get("display_name")):
            raise CommentWriteUncertain("待回写评论内容或署名发生变化，请核对本地运行记录")
        if operation.get("attempted"):
            matches = [c for c in comments if c["id"] not in operation["baseline_ids"]
                       and text_of(c.get("rich_text", [])) == message
                       and c.get("created_by", {}).get("id") in operation.get("actor_ids", [operation["actor_id"]])
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
