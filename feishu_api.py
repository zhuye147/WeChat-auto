"""
飞书电子表格 API 模块
使用飞书 Sheets API 读写电子表格数据
"""
import os
import json
import time
import logging
import requests

logger = logging.getLogger(__name__)

# 列索引映射（0-based）
COL_MAP = {
    '学员编号': 0, '姓名': 1, '班级编号': 2, '订单号': 3,
    '业务办理': 4, '备注': 5, '群聊': 6, '需求人': 7,
    '办理人': 8, '是否办理完成': 9, '办理情况': 10, '办理情况2': 11,
    '是否群内回复': 12
}


def _is_yes(value) -> bool:
    """单元格值是否为"是"（容忍 None / 首尾空白 / 全角空格等）"""
    return str(value or '').strip() == '是'


def _record_fingerprint(record: dict) -> tuple:
    """记录内容指纹：同一学员多行需求时，用业务字段区分不同的一笔业务"""
    keys = ('备注', '业务办理', '办理情况', '办理情况2')
    return tuple(str(record.get(k) or '').strip() for k in keys)


class FeishuAPI:
    """飞书电子表格操作 API"""

    def __init__(self, app_id: str, app_secret: str, spreadsheet_token: str, sheet_id: str = None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.spreadsheet_token = spreadsheet_token
        self._sheet_id = sheet_id
        self._tenant_token = None
        self._token_expire = 0

    # ==================== 认证 ====================

    def _get_tenant_access_token(self) -> str:
        """获取飞书 tenant_access_token（自动缓存刷新）"""
        if self._tenant_token and time.time() < self._token_expire:
            return self._tenant_token

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        resp = requests.post(url, json={
            "app_id": self.app_id,
            "app_secret": self.app_secret
        }, timeout=10)

        if resp.status_code != 200:
            raise Exception(f"获取 token 失败: {resp.status_code} {resp.text}")

        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"获取 token 失败: {data}")

        self._tenant_token = data["tenant_access_token"]
        self._token_expire = time.time() + data.get("expire", 7200) - 60
        return self._tenant_token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_tenant_access_token()}",
            "Content-Type": "application/json"
        }

    # ==================== 工作表操作 ====================

    def _get_sheet_id(self) -> str:
        """获取电子表格的第一个工作表 ID"""
        if self._sheet_id:
            return self._sheet_id

        url = f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{self.spreadsheet_token}/sheets/query"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"查询工作表失败: {data}")

        sheets = data.get("data", {}).get("sheets", [])
        if not sheets:
            raise Exception("电子表格中没有工作表")

        self._sheet_id = sheets[0]["sheet_id"]
        logger.info(f"获取到工作表 ID: {self._sheet_id}")
        return self._sheet_id

    def _get_next_row(self, max_retries=3) -> int:
        """获取第一个整行为空的空行行号

        读取 A:M 全列逐行判断，而不是数 A 列长度：
        表格被人删除/清空行、或某行学员编号为空时，数 A 列会算错行号，导致新记录覆盖已有记录。
        """
        sheet_id = self._get_sheet_id()
        range_str = f"{sheet_id}!A:M"

        for attempt in range(max_retries):
            try:
                url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{self.spreadsheet_token}/values/{range_str}"
                resp = requests.get(url, headers=self._headers(), timeout=10)
                data = resp.json()

                if data.get("code") != 0:
                    raise Exception(f"查询失败: {data}")

                values = data.get("data", {}).get("valueRange", {}).get("values", [])
                for i, row in enumerate(values):
                    if not row or all(str(c or '').strip() == '' for c in row):
                        return i + 1  # 第一个整行为空的行
                return len(values) + 1

            except Exception as e:
                logger.warning(f"获取空行失败 (尝试 {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    raise

    # ==================== 写入数据 ====================

    def write_record(self, record: dict, row: int = None) -> bool:
        """写入单条记录到电子表格"""
        if row is None:
            row = self._get_next_row()

        sheet_id = self._get_sheet_id()
        range_str = f"{sheet_id}!A{row}:H{row}"

        values = [[
            record.get('学员编号', ''),
            record.get('姓名', ''),
            record.get('班级编号', ''),
            record.get('订单号', ''),
            record.get('业务办理', ''),
            record.get('备注', ''),
            record.get('群聊', ''),
            record.get('发送人', ''),
        ]]

        url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{self.spreadsheet_token}/values"
        body = {
            "valueRange": {
                "range": range_str,
                "values": values
            }
        }

        resp = requests.put(url, headers=self._headers(), json=body, timeout=10)
        data = resp.json()

        if data.get("code") == 0:
            logger.info(f"写入成功: 第{row}行 {record.get('姓名', '')}")
            return True
        else:
            logger.error(f"写入失败: {data}")
            return False

    def write_records_batch(self, records: list) -> list:
        """批量写入记录到电子表格，返回每条记录对应的行号列表（失败为 None）"""
        if not records:
            return []

        row = self._get_next_row()
        sheet_id = self._get_sheet_id()
        results = []

        for i, record in enumerate(records):
            if self.write_record(record, row + i):
                results.append(row + i)
            else:
                results.append(None)
            time.sleep(0.3)  # 避免频率限制

        return results

    # ==================== 读取数据 ====================

    def read_all_records(self) -> list:
        """读取表格所有行，返回记录列表（含行号）"""
        sheet_id = self._get_sheet_id()
        # 读取 A 到 M 列（13列）
        range_str = f"{sheet_id}!A:M"

        url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{self.spreadsheet_token}/values/{range_str}"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"读取表格失败：{data}")

        values = data.get("data", {}).get("valueRange", {}).get("values", [])
        if not values:
            return []

        # 第一行是表头，跳过
        records = []
        for i, row in enumerate(values[1:], start=2):  # 行号从2开始
            record = {
                '_row': i,
                '学员编号': row[0] if len(row) > 0 else '',
                '姓名': row[1] if len(row) > 1 else '',
                '班级编号': row[2] if len(row) > 2 else '',
                '订单号': row[3] if len(row) > 3 else '',
                '业务办理': row[4] if len(row) > 4 else '',
                '备注': row[5] if len(row) > 5 else '',
                '群聊': row[6] if len(row) > 6 else '',
                '需求人': row[7] if len(row) > 7 else '',
                '办理人': row[8] if len(row) > 8 else '',
                '是否办理完成': row[9] if len(row) > 9 else '',
                '办理情况': row[10] if len(row) > 10 else '',
                '办理情况2': row[11] if len(row) > 11 else '',
                '是否群内回复': row[12] if len(row) > 12 else '',
            }
            records.append(record)

        logger.info(f"读取到 {len(records)} 条记录")
        return records

    def extract_images_and_text(self, content):
        """
        从"办理情况"单元格值中提取图片 token 列表和文字内容。

        兼容多种单元格格式：
        - 纯文字（str）：无图片，原文返回
        - dict {'fileToken': '...', 'text': '...'}：单张图片
        - dict {'fileTokens': ['...', '...'], 'text': '...'}：多张图片
        - list [dict, dict, ...] 或 [dict, '文字', ...]：多张图片
        - 字符串形式的 JSON 数组/对象

        Returns:
            (file_tokens: list[str], text: str)
        """
        tokens, texts = [], []

        def handle_dict(item):
            for key in ('fileToken', 'file_token', 'token'):
                t = item.get(key)
                if t and str(t).strip():
                    tokens.append(str(t).strip())
                    break
            for key in ('fileTokens', 'file_tokens', 'imageTokens'):
                for t in (item.get(key) or []):
                    if t and str(t).strip():
                        tokens.append(str(t).strip())
            if item.get('text'):
                texts.append(str(item['text']).strip())

        def handle_value(value):
            if isinstance(value, dict):
                handle_dict(value)
            elif isinstance(value, list):
                for v in value:
                    handle_value(v)
            elif isinstance(value, str):
                s = value.strip()
                if not s:
                    return
                if s.startswith('[') and s.endswith(']'):
                    try:
                        arr = json.loads(s)
                        if isinstance(arr, list):
                            handle_value(arr)
                            return
                    except Exception:
                        pass
                if s.startswith('{') and s.endswith('}'):
                    try:
                        obj = json.loads(s)
                        if isinstance(obj, dict):
                            handle_dict(obj)
                            return
                    except Exception:
                        pass
                texts.append(s)

        handle_value(content)
        return tokens, '\n'.join(t for t in texts if t)

    def locate_pending_record(self, record: dict):
        """
        在最新表格中重新定位一条待回复记录（学员编号+姓名+群聊匹配）。

        同一学员可能对同一笔业务修改过需求，表格中会保留多行（旧行已回复、
        新行待回复），此时按"是否已回复"即可消歧；仍分不清（多行都待回复）时
        再用业务字段内容指纹与快照比对，唯一命中才处理。

        Args:
            record: 从 read_all_records 得到的记录快照

        Returns:
            定位到且状态仍为"办理完成=是 且 未群内回复"的新记录；否则返回 None
        """
        key_id = (record.get('学员编号') or '').strip()
        key_name = (record.get('姓名') or '').strip()
        key_group = (record.get('群聊') or '').strip()

        matched = []
        for r in self.read_all_records():
            if (r.get('姓名') or '').strip() != key_name or (r.get('群聊') or '').strip() != key_group:
                continue
            if key_id and (r.get('学员编号') or '').strip() != key_id:
                continue
            matched.append(r)

        if not matched:
            logger.warning(f"表格中未找到记录: 姓名={key_name!r} 群聊={key_group!r} 编号={key_id!r}")
            return None

        # 只看仍待回复的行：旧行若已回复过，不参与定位
        pending = [r for r in matched
                   if _is_yes(r.get('是否办理完成'))
                   and str(r.get('是否群内回复') or '').strip() != '是']

        if not pending:
            logger.info(f"记录状态已变化，无需再处理: 姓名={key_name!r} 群聊={key_group!r} "
                        f"编号={key_id!r}")
            return None
        if len(pending) == 1:
            return pending[0]

        # 同一学员多行都待回复（几笔独立需求）：按内容指纹找快照对应的那一行
        fp = _record_fingerprint(record)
        same = [r for r in pending if _record_fingerprint(r) == fp]
        if len(same) == 1:
            return same[0]
        logger.warning(f"同人同群同编号有 {len(pending)} 行都待回复且无法按内容区分，跳过: "
                       f"姓名={key_name!r} 群聊={key_group!r} 编号={key_id!r}")
        return None

    def update_cell(self, row: int, col_index: int, value: str) -> bool:
        """更新指定单元格的值"""
        sheet_id = self._get_sheet_id()
        # col_index 是 0-based，转换为列字母（A=0, B=1, ...）
        col_letter = chr(ord('A') + col_index)
        range_str = f"{sheet_id}!{col_letter}{row}:{col_letter}{row}"

        url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{self.spreadsheet_token}/values"
        body = {
            "valueRange": {
                "range": range_str,
                "values": [[value]]
            }
        }

        resp = requests.put(url, headers=self._headers(), json=body, timeout=10)
        data = resp.json()

        if data.get("code") == 0:
            logger.info(f"更新成功：第{row}行，列{col_letter} = {value}")
            return True
        else:
            logger.error(f"更新失败：{data}")
            return False

    def download_image(self, file_token: str, save_dir: str = ".") -> str:
        """
        下载飞书电子表格中的图片，返回本地文件路径。
        支持 image_ 前缀的媒体 token 和 Drive 文件 token。
        """
        token = file_token.strip().strip('"').strip("'")
        if not token:
            logger.warning(f"空的图片token")
            return None

        url = f"https://open.feishu.cn/open-apis/drive/v1/medias/{token}/download"
        headers = {"Authorization": f"Bearer {self._get_tenant_access_token()}"}

        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            logger.error(f"下载图片失败：HTTP {resp.status_code} token={token[:20]}...")
            return None

        os.makedirs(save_dir, exist_ok=True)
        # 根据 Content-Type 判断扩展名
        ct = resp.headers.get('Content-Type', '')
        ext = '.png'
        if 'jpeg' in ct or 'jpg' in ct:
            ext = '.jpg'
        elif 'gif' in ct:
            ext = '.gif'
        elif 'bmp' in ct:
            ext = '.bmp'
        elif 'webp' in ct:
            ext = '.webp'

        file_path = os.path.join(save_dir, f"{token}{ext}")
        with open(file_path, 'wb') as f:
            f.write(resp.content)

        logger.info(f"图片已下载：{file_path} ({len(resp.content)} bytes)")
        return file_path

    # ==================== 连接测试 ====================

    def test_connection(self) -> bool:
        """测试飞书 API 连接是否正常"""
        try:
            token = self._get_tenant_access_token()
            logger.info("获取 token 成功")
            sheet_id = self._get_sheet_id()
            logger.info(f"获取工作表成功: {sheet_id}")
            return True
        except Exception as e:
            logger.error(f"连接测试失败: {e}")
            return False
