"""
ERP Adapter layer — swap ERPNext for another ERP by replacing ERPNextAdapter.
The agent and tools only talk to ERPAdapter (abstract interface).
"""
from abc import ABC, abstractmethod
from typing import Any
import httpx


# ── Abstract interface ────────────────────────────────────────────────────────

class ERPAdapter(ABC):
    @abstractmethod
    def list(self, doctype: str, filters=None, fields=None,
             limit: int = 20, order_by: str = None,
             group_by: str = None, sum_field: str = None) -> dict: ...

    @abstractmethod
    def get(self, doctype: str, name: str) -> dict: ...

    @abstractmethod
    def search(self, doctype: str, query: str) -> list: ...

    @abstractmethod
    def get_fields(self, doctype: str) -> list: ...

    @abstractmethod
    def linked(self, source_doctype: str, source_name: str,
               target_doctype: str, fields: list = None) -> list: ...

    @abstractmethod
    def list_items(self, parent_doctype: str, filters=None,
                   group_by: str = "item_code", sum_field: str = "amount") -> dict: ...

    @abstractmethod
    def run_report(self, report_name: str, filters: dict = None, top_n: int = 20) -> dict: ...

    @abstractmethod
    def compare_items(self, doctype_a: str, doctype_b: str,
                      filters_a=None, filters_b=None,
                      group_by: str = "item_code", sum_field: str = "qty") -> dict: ...

    # ── Write operations ─────────────────────────────────────────────────────
    @abstractmethod
    def create(self, doctype: str, data: dict) -> dict: ...

    @abstractmethod
    def update(self, doctype: str, name: str, data: dict) -> dict: ...

    @abstractmethod
    def submit(self, doctype: str, name: str) -> dict: ...

    @abstractmethod
    def cancel(self, doctype: str, name: str) -> dict: ...

    @abstractmethod
    def execute_sql(self, sql_query: str) -> list: ...


# ── ERPNext implementation ────────────────────────────────────────────────────

class ERPNextAdapter(ERPAdapter):
    def __init__(self, url: str, api_key: str = None, api_secret: str = None,
                 cookies: dict = None, csrf_token: str = None):
        self.url = url.rstrip("/")
        self.cookies = cookies or {}
        self.csrf_token = csrf_token or ""
        # Cookie auth: no Authorization header needed
        if api_key and api_secret:
            self.headers = {"Authorization": f"token {api_key}:{api_secret}"}
        else:
            self.headers = {}

    def _get(self, path: str, params: dict = None) -> Any:
        # Encode spaces in path so "Sales Invoice" → "Sales%20Invoice"
        encoded_path = path.replace(" ", "%20")
        r = httpx.get(
            f"{self.url}{encoded_path}",
            headers=self.headers,
            cookies=self.cookies or None,
            params=params,
            timeout=30,
        )
        if not r.is_success:
            raise Exception(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def _fetch_page(self, doctype, filters, fields, limit, start, order_by) -> list:
        import json
        params = {
            "limit_page_length": limit,
            "limit_start": start,
            "fields": json.dumps(fields or ["name"]),
        }
        if filters:
            params["filters"] = json.dumps(filters)
        if order_by:
            params["order_by"] = order_by
        return self._get(f"/api/resource/{doctype}", params).get("data", [])

    def list(self, doctype, filters=None, fields=None,
             limit=20, order_by=None,
             group_by=None, sum_field=None) -> dict:

        if group_by:
            # Ensure group_by and sum_field are included in fetched fields
            fetch_fields = list(fields) if fields else []
            for f in [group_by, sum_field]:
                if f and f not in fetch_fields:
                    fetch_fields.append(f)

            # Fetch ALL pages internally, aggregate in Python, return summary
            all_rows, page_size, start = [], 200, 0
            while True:
                page = self._fetch_page(doctype, filters, fetch_fields, page_size, start, order_by)
                all_rows.extend(page)
                if len(page) < page_size:
                    break
                start += page_size

            from collections import defaultdict
            totals = defaultdict(float)
            counts = defaultdict(int)
            for row in all_rows:
                key = row.get(group_by, "Unknown")
                totals[key] += float(row.get(sum_field, 0) or 0)
                counts[key] += 1

            summary = sorted(
                [{"group": k, sum_field: round(v, 2), "count": counts[k]}
                 for k, v in totals.items()],
                key=lambda x: -x[sum_field],
            )
            top = summary[0] if summary else None
            hint = (
                f"Top result: group='{top['group']}' {sum_field}={top[sum_field]}. "
                f"To drill down into this group, add a filter on '{group_by}' = '{top['group']}' in your next call."
            ) if top else "No results found."
            return {
                "data": summary,
                "total_records_fetched": len(all_rows),
                "hint": hint,
            }

        else:
            # Normal list with truncation warning
            rows = self._fetch_page(doctype, filters, fields, limit, 0, order_by)
            result = {"data": rows, "count": len(rows)}
            if len(rows) >= limit:
                # Fetch real total count so LLM knows the true number
                try:
                    import json as _json
                    count_params = {"doctype": doctype}
                    if filters:
                        count_params["filters"] = _json.dumps(filters)
                    count_data = self._get("/api/method/frappe.client.get_count", count_params)
                    total_count = count_data.get("message")
                except Exception:
                    total_count = None

                if total_count:
                    result["total_count"] = total_count
                    result["warning"] = (
                        f"Results truncated at {limit} rows (total: {total_count}). "
                        "Use filters to narrow down or group_by for aggregation."
                    )
                else:
                    result["warning"] = (
                        f"Results truncated at {limit} rows — there are more records. "
                        "Use filters to narrow down or group_by for aggregation."
                    )
            return result

    def get(self, doctype, name) -> dict:
        data = self._get(f"/api/resource/{doctype}/{name}")
        return data.get("data", {})

    def search(self, doctype, query) -> list:
        data = self._get("/api/method/frappe.desk.search.search_link", {
            "doctype": doctype,
            "txt": query,
            "page_length": 10,
        })
        return data.get("message", [])

    def linked(self, source_doctype: str, source_name: str,
               target_doctype: str, fields: list = None) -> list:
        """
        Find target documents linked to source via Frappe child table filter.
        Tries common link field naming patterns automatically.
        """
        child_doctype = f"{target_doctype} Item"
        base_field   = source_doctype.lower().replace(" ", "_")   # sales_order
        candidates   = [base_field, f"against_{base_field}"]      # + against_sales_order

        default_fields = ["name", "status", "grand_total", "posting_date"]

        for field in candidates:
            try:
                rows = self.list(
                    doctype=target_doctype,
                    filters=[[child_doctype, field, "=", source_name]],
                    fields=fields or default_fields,
                )
                if rows is not None:   # empty list is still a valid (no results) answer
                    return rows
            except Exception:
                continue
        return []

    def list_items(self, parent_doctype: str, filters=None,
                   group_by: str = "item_code", sum_field: str = "amount") -> dict:
        """
        Aggregate line items from parent documents.
        Lists all matching parent docs, fetches each full doc, aggregates items in Python.
        Works around Frappe child-doctype permission restrictions.
        """
        # Step 1: list parent doc names (all pages)
        all_names, page_size, start = [], 200, 0
        while True:
            page = self._fetch_page(parent_doctype, filters, ["name"], page_size, start, None)
            all_names.extend(r["name"] for r in page)
            if len(page) < page_size:
                break
            start += page_size

        # Step 2: fetch each full doc and aggregate items
        from collections import defaultdict
        totals = defaultdict(float)
        counts = defaultdict(int)
        labels = {}   # item_code → item_name

        for name in all_names:
            try:
                doc = self.get(parent_doctype, name)
                for item in doc.get("items", []):
                    key = item.get(group_by) or item.get("item_code", "Unknown")
                    val = float(item.get(sum_field, 0) or 0)
                    totals[key] += val
                    counts[key] += 1
                    if key not in labels and item.get("item_name"):
                        labels[key] = item["item_name"]
            except Exception:
                continue

        summary = sorted(
            [{"group": k, sum_field: round(v, 2), "count": counts[k],
              "item_name": labels.get(k, k)}
             for k, v in totals.items()],
            key=lambda x: -x[sum_field],
        )
        return {
            "data": summary,
            "total_parents_fetched": len(all_names),
            "group_by": group_by,
            "sum_field": sum_field,
        }

    def run_report(self, report_name: str, filters: dict = None, top_n: int = 20) -> dict:
        import json
        data = self._get("/api/method/frappe.desk.query_report.run", {
            "report_name": report_name,
            "filters": json.dumps(filters or {}),
        })
        result = data.get("message", {})
        columns = [c.get("label") or c.get("fieldname") for c in result.get("columns", [])]
        rows = result.get("result", [])
        # Strip summary/total rows (ERPNext appends them with bold flag)
        rows = [r for r in rows if not (isinstance(r, dict) and r.get("bold"))]
        total_rows = len(rows)

        # Sort by largest numeric value found in each row, then truncate to top_n
        def row_sort_key(row):
            if isinstance(row, dict):
                # Prefer explicit total/grand_total fields
                for key in ("total", "grand_total", "amount", "base_amount"):
                    if isinstance(row.get(key), (int, float)):
                        return row[key]
                vals = [v for v in row.values() if isinstance(v, (int, float))]
            else:
                vals = [v for v in row if isinstance(v, (int, float))]
            return max(vals) if vals else 0

        rows = sorted(rows, key=row_sort_key, reverse=True)[:top_n]
        return {
            "columns": columns,
            "rows": rows,
            "returned": len(rows),
            "total": total_rows,
            **({"truncated": True, "hint": f"Showing top {top_n} of {total_rows} rows."} if total_rows > top_n else {}),
        }

    def compare_items(self, doctype_a: str, doctype_b: str,
                      filters_a=None, filters_b=None,
                      group_by: str = "item_code", sum_field: str = "qty") -> dict:
        """
        Compare item sets across two parent doctypes.
        Returns items only in A, only in B, and in both — Python does the set math.
        """
        def fetch(doctype, filters):
            totals, labels = {}, {}
            all_names, page_size, start = [], 200, 0
            while True:
                page = self._fetch_page(doctype, filters, ["name"], page_size, start, None)
                all_names.extend(r["name"] for r in page)
                if len(page) < page_size:
                    break
                start += page_size
            for name in all_names:
                try:
                    doc = self.get(doctype, name)
                    for item in doc.get("items", []):
                        key = item.get(group_by, "Unknown")
                        totals[key] = totals.get(key, 0) + float(item.get(sum_field, 0) or 0)
                        labels[key] = item.get("item_name", key)
                except Exception:
                    continue
            return totals, labels

        set_a, labels_a = fetch(doctype_a, filters_a)
        set_b, labels_b = fetch(doctype_b, filters_b)

        keys_a, keys_b = set(set_a), set(set_b)
        only_a = sorted(keys_a - keys_b)
        only_b = sorted(keys_b - keys_a)
        in_both = sorted(keys_a & keys_b)

        labels = {**labels_b, **labels_a}
        return {
            f"only_in_{doctype_a.lower().replace(' ','_')}": [
                {"item_code": k, "item_name": labels.get(k, k), sum_field: round(set_a[k], 2)}
                for k in only_a
            ],
            f"only_in_{doctype_b.lower().replace(' ','_')}": [
                {"item_code": k, "item_name": labels.get(k, k), sum_field: round(set_b[k], 2)}
                for k in only_b
            ],
            "in_both": len(in_both),
            f"total_{doctype_a.lower().replace(' ','_')}_items": len(keys_a),
            f"total_{doctype_b.lower().replace(' ','_')}_items": len(keys_b),
        }

    # ── Write operations ─────────────────────────────────────────────────────

    def _write_headers(self) -> dict:
        h = {**self.headers, "Content-Type": "application/json"}
        if self.csrf_token:
            h["X-Frappe-CSRF-Token"] = self.csrf_token
        return h

    def _post(self, path: str, data: dict = None) -> dict:
        encoded_path = path.replace(" ", "%20")
        r = httpx.post(
            f"{self.url}{encoded_path}",
            headers=self._write_headers(),
            cookies=self.cookies or None,
            json=data or {},
            timeout=30,
        )
        if not r.is_success:
            raise Exception(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def _put(self, path: str, data: dict = None) -> dict:
        encoded_path = path.replace(" ", "%20")
        r = httpx.put(
            f"{self.url}{encoded_path}",
            headers=self._write_headers(),
            cookies=self.cookies or None,
            json=data or {},
            timeout=30,
        )
        if not r.is_success:
            raise Exception(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def create(self, doctype: str, data: dict) -> dict:
        result = self._post(f"/api/resource/{doctype}", data)
        return result.get("data", {})

    def update(self, doctype: str, name: str, data: dict) -> dict:
        result = self._put(f"/api/resource/{doctype}/{name}", data)
        return result.get("data", {})

    def submit(self, doctype: str, name: str) -> dict:
        return self._post("/api/method/frappe.client.submit", {
            "doc": {"doctype": doctype, "name": name},
        })

    def cancel(self, doctype: str, name: str) -> dict:
        return self._post("/api/method/frappe.client.cancel", {
            "doctype": doctype, "name": name,
        })

    def upload_file(self, file_bytes: bytes, filename: str,
                    doctype: str, docname: str, is_private: int = 1) -> dict:
        upload_headers = {**self.headers}
        if self.csrf_token:
            upload_headers["X-Frappe-CSRF-Token"] = self.csrf_token
        r = httpx.post(
            f"{self.url}/api/method/upload_file",
            headers=upload_headers,
            cookies=self.cookies or None,
            files={"file": (filename, file_bytes)},
            data={"doctype": doctype, "docname": docname, "is_private": is_private},
            timeout=30,
        )
        if not r.is_success:
            raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json().get("message", {})

    def get_fields(self, doctype) -> list:
        data = self._get("/api/method/frappe.client.get_meta", {
            "doctype": doctype,
        })
        docs = data.get("docs", [])
        if not docs:
            return []
        skip = {"Section Break", "Column Break", "Tab Break", "HTML", "Heading"}
        return [
            {
                "fieldname": f.get("fieldname"),
                "label":     f.get("label"),
                "fieldtype": f.get("fieldtype"),
                "options":   f.get("options"),
                "required":  bool(f.get("reqd", 0)),
            }
            for f in docs[0].get("fields", [])
            if f.get("fieldtype") not in skip
        ]

    def execute_sql(self, sql_query: str) -> list:
        try:
            data = self._post("/api/method/execute_raw_sql", {"sql_query": sql_query})
            if "message" in data and isinstance(data["message"], dict) and "error" in data["message"]:
                 return [{"error": data["message"]["error"]}]
            return data.get("message", [])
        except Exception as e:
            return [{"error": str(e)}]


# ── Factory ───────────────────────────────────────────────────────────────────

def get_erp_adapter() -> ERPAdapter:
    import config
    if config.ERP_PROVIDER == "erpnext":
        return ERPNextAdapter(
            config.ERPNEXT_URL,
            config.ERPNEXT_API_KEY,
            config.ERPNEXT_SECRET,
        )
    raise ValueError(f"Unknown ERP provider: {config.ERP_PROVIDER}")


def get_erp_adapter_cookie(cookies: dict, csrf_token: str = "") -> ERPAdapter:
    import config
    return ERPNextAdapter(config.ERPNEXT_URL, cookies=cookies, csrf_token=csrf_token)
