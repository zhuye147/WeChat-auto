"""  
微信消息监听模块 - 基于 wxauto4
通过 wxauto4 直接读取消息（含发送人），无需 OCR 或自定义 UIA 滚动
"""
import re
import sys
import types
import os
import json
import time
import hashlib
import logging
import ctypes
from typing import List, Dict

# Mock win32ui（wxauto4 导入了但未实际使用，系统缺少 MFC DLL）
sys.modules['win32ui'] = types.ModuleType('win32ui')

from wxauto4 import WeChat

logger = logging.getLogger(__name__)

# 默认筛选词：仅为占位，实际应在 config.yaml 的 wechat.bot_aliases / wechat.keywords 中配置
DEFAULT_BOT_ALIASES = ['客服小助手']
DEFAULT_KEYWORDS = ['业务办理']


class WeChatUIA:
    """微信群消息监听器（wxauto4 版）"""

    def __init__(self, groups: List[str], state_file: str = "processed_state.json",
                 bot_aliases: List[str] = None, keywords: List[str] = None):
        """
        Args:
            groups: 需要监听的群名称列表
            state_file: 消息去重状态文件路径
            bot_aliases: 消息中必须提到的机器人/客服昵称（对应 config.yaml 的 wechat.bot_aliases）
            keywords: 消息还必须包含的业务关键字（对应 config.yaml 的 wechat.keywords）
        """
        self.groups = groups
        self.state_file = state_file
        self.bot_aliases = [a for a in (bot_aliases or []) if a] or list(DEFAULT_BOT_ALIASES)
        self.keywords = [k for k in (keywords or []) if k] or list(DEFAULT_KEYWORDS)
        self._seen_hashes = set()
        self._wx = None
        self._session_controls = {}  # 群名 -> UI控件映射，用于直接点击

        self._load_state()

    # ==================== 初始化与连接 ====================

    def _init_wx(self) -> bool:
        """初始化 wxauto4 实例"""
        try:
            # resize=False：禁止 wxauto4 自动设定窗口尺寸，保留用户手动调整的大小
            self._wx = WeChat(ads=False, resize=False)
            logger.info(f"微信窗口已连接: {self._wx.nickname}")
            self._enlarge_window()
            return True
        except Exception as e:
            logger.warning(f"微信连接失败: {e}")
            self._wx = None
            return False

    def _enlarge_window(self):
        """启动时将微信窗口调整为屏幕可用区域的大尺寸并居中，方便查看消息"""
        try:
            import win32api
            import win32gui
            from wxauto4.uia import uiautomation as uia
            main_wnd = uia.WindowControl(Name='微信')
            if not main_wnd.Exists(2):
                return
            sw = win32api.GetSystemMetrics(0)  # 屏幕宽
            sh = win32api.GetSystemMetrics(1)  # 屏幕高
            # 左上角顶格：宽为屏幕2/3，高为整个屏幕
            w, h = int(sw * 2 / 3), sh
            x, y = 0, 0
            hwnd = main_wnd.NativeWindowHandle
            win32gui.MoveWindow(hwnd, x, y, w, h, True)
            logger.info(f"微信窗口已调整为 {w}x{h} (屏幕 {sw}x{sh})")
        except Exception as e:
            logger.debug(f"调整微信窗口大小失败: {e}")

    def check_wechat_running(self) -> bool:
        """检查微信是否在运行"""
        if self._wx:
            try:
                # 尝试访问窗口确认仍有效
                _ = self._wx.nickname
                return True
            except:
                pass
        return self._init_wx()

    # ==================== 未读群检测 ====================

    def _get_unread_groups(self) -> List[str]:
        """
        扫描左侧会话列表，返回有未读消息的监控群名称。
        通过会话项 Name 中的 [X条] 标识判断未读。
        兼容旧版微信（ListControl）和新版微信（Qt UI）。
        """
        try:
            from wxauto4.uia import uiautomation as uia
            main_wnd = uia.WindowControl(Name='微信')
            if not main_wnd.Exists(2):
                return self.groups

            # 确保在会话列表页，并将会话列表滚动到顶部
            # （微信会话列表是虚拟列表：列表沉底时上方监控群不在控件树中，会漏采；
            #   且无未读时不会走 _navigate_to_chat_list，必须在这里兜底）
            wx_btn = main_wnd.ButtonControl(Name='微信', ClassName='mmui::XTabBarItem')
            if wx_btn.Exists(2):
                wx_btn.Click()
                time.sleep(0.3)
            self._scroll_session_list_to_top(main_wnd)

            unread_groups = []

            # 方法1：旧版微信 - 查找 ListControl(Name='会话')
            session_list = main_wnd.ListControl(Name='会话')
            if session_list.Exists(2):
                unread_groups = self._scan_session_items(session_list.GetChildren())
                if unread_groups:
                    logger.debug(f"未读群(旧版UI): {unread_groups}")
                else:
                    logger.debug("无未读群")
                return unread_groups

            # 方法2：新版微信 - 在 XSplitterView 中查找会话项
            logger.debug("旧版会话列表未找到，尝试新版UI结构...")
            unread_groups = self._scan_new_ui_sessions(main_wnd)
            if unread_groups:
                logger.debug(f"未读群(新版UI): {unread_groups}")
            else:
                logger.debug("无未读群")
            return unread_groups

        except Exception as e:
            logger.warning(f"检测未读群失败: {e}，本轮跳过所有群")
            return []

    def _scan_session_items(self, items) -> List[str]:
        """扫描会话列表项，返回有未读的监控群"""
        unread_groups = []
        self._session_controls.clear()
        for item in items:
            name = item.Name or ''
            first_line = name.split('\n')[0].strip()

            matched_group = self._match_group_name(first_line)
            if not matched_group:
                continue

            # 记住UI控件，用于直接点击
            self._session_controls[matched_group] = item

            if re.search(r'\[\d+条\]', name):
                unread_groups.append(matched_group)

        return unread_groups

    def _scan_new_ui_sessions(self, main_wnd) -> List[str]:
        """新版微信UI：遍历控件树查找含未读标识的会话项"""
        try:
            from wxauto4.uia import uiautomation as uia
            # 查找 XSplitterView（左侧会话区域）
            splitter = main_wnd.FindControl(
                searchDepth=5,
                ControlType=uiautomation.ControlType.CustomControl,
                ClassName='mmui::XSplitterView'
            )
            if not splitter:
                logger.debug("未找到 XSplitterView")
                return []

            # 在 splitter 中查找所有可能包含会话名的控件
            unread_groups = []
            self._session_controls.clear()
            # 搜索深度限制为 6，避免遍历整个窗口树
            self._walk_control_tree(splitter, unread_groups, max_depth=6, current_depth=0)
            return unread_groups

        except Exception as e:
            logger.debug(f"新版UI扫描失败: {e}")
            return []

    def _walk_control_tree(self, ctrl, unread_groups, max_depth, current_depth):
        """递归遍历控件树，查找含未读标识的监控群"""
        if current_depth > max_depth:
            return

        name = ctrl.Name or ''
        first_line = name.split('\n')[0].strip()
        
        # 匹配监控群名，记住UI控件
        matched = self._match_group_name(first_line)
        if matched:
            self._session_controls[matched] = ctrl

        # 检查是否包含未读标识 [X条]
        if re.search(r'\[\d+条\]', name):
            if matched and matched not in unread_groups:
                unread_groups.append(matched)

        # 继续遍历子控件
        try:
            for child in ctrl.GetChildren():
                self._walk_control_tree(child, unread_groups, max_depth, current_depth + 1)
        except Exception:
            pass

    def _match_group_name(self, display_name: str) -> str:
        """模糊匹配群名：去掉 emoji 和人数后缀后比对"""
        for g in self.groups:
            g_clean = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f\u200d]', '', g).strip()
            dn_clean = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f\u200d]', '', display_name).strip()
            if g_clean in dn_clean or dn_clean == g_clean:
                return g
        return None
    
    def _scan_all_sessions(self):
        """扫描会话列表中所有群（不论是否有未读），填充 _session_controls"""
        try:
            from wxauto4.uia import uiautomation as uia
            main_wnd = uia.WindowControl(Name='微信')
            if not main_wnd.Exists(2):
                return
    
            self._session_controls.clear()
    
            # 旧版微信
            session_list = main_wnd.ListControl(Name='会话')
            if session_list.Exists(2):
                for item in session_list.GetChildren():
                    name = item.Name or ''
                    first_line = name.split('\n')[0].strip()
                    matched = self._match_group_name(first_line)
                    if matched:
                        self._session_controls[matched] = item
                return
    
            # 新版微信
            splitter = main_wnd.FindControl(
                searchDepth=5,
                ControlType=uiautomation.ControlType.CustomControl,
                ClassName='mmui::XSplitterView'
            )
            if splitter:
                self._walk_all_controls(splitter, max_depth=6, current_depth=0)
    
        except Exception as e:
            logger.debug(f"扫描所有群失败：{e}")
    
    def _walk_all_controls(self, ctrl, max_depth, current_depth):
        """递归遍历控件树，记住所有匹配的群"""
        if current_depth > max_depth:
            return
    
        name = ctrl.Name or ''
        first_line = name.split('\n')[0].strip()
        matched = self._match_group_name(first_line)
        if matched:
            self._session_controls[matched] = ctrl
    
        try:
            for child in ctrl.GetChildren():
                self._walk_all_controls(child, max_depth, current_depth + 1)
        except Exception:
            pass

    def _strip_emoji(self, text: str) -> str:
        """去掉字符串中的emoji字符，用于微信搜索"""
        return re.sub(r'[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f\u200d]', '', text).strip()

    def _get_current_chat_name(self):
        """读取微信当前打开的会话名；读不到返回 None。

        新版微信聊天页底部的输入框控件(mmui::ChatInputField)的 Name
        就是当前会话名（实测：打开哪个群，输入框 Name 就是哪个群名）。
        旧版微信没有该控件时，退回用通用输入框查找兜底。
        """
        try:
            from wxauto4.uia import uiautomation as uia
            main_wnd = uia.WindowControl(Name='微信')
            if not main_wnd.Exists(2):
                return None

            def walk(ctrl, depth):
                if depth > 16:
                    return None
                try:
                    if (ctrl.ClassName or '') == 'mmui::ChatInputField':
                        return ctrl
                    for ch in ctrl.GetChildren():
                        r = walk(ch, depth + 1)
                        if r is not None:
                            return r
                except Exception:
                    pass
                return None

            ctrl = walk(main_wnd, 0)
            if ctrl is None:
                ctrl = self._find_chat_input(main_wnd)  # 旧版微信兜底
            if ctrl is None:
                return None
            name = (ctrl.Name or '').split('\n')[0].strip()
            if not name:
                return None
            return self._match_group_name(name) or name
        except Exception as e:
            logger.debug(f"读取当前会话名失败: {e}")
            return None

    def _enter_group(self, group_name: str, wait_after_click: float = 0.8, attempts: int = 3) -> bool:
        """进入指定群聊：扫描会话列表 → 点击 → 验证当前打开的确实就是该群。

        微信会话列表是虚拟列表，新消息会把会话顶起、旧控件坐标随之错位，
        直接点击可能打开别的群。点击后必须验证当前会话名，不匹配则丢弃
        旧控件重新扫描重试，防止把 A 群的消息/回复记到 B 群头上。

        Args:
            group_name: 群名（配置规范名或表格里的近似名，内部自动规范化）
            wait_after_click: 点击后等待秒数
            attempts: 最多尝试次数

        Returns:
            bool: 是否确认进入了该群
        """
        canonical = self._match_group_name(group_name) or group_name
        for attempt in range(attempts):
            if canonical not in self._session_controls:
                self._scan_all_sessions()
            if canonical in self._session_controls:
                try:
                    self._session_controls[canonical].Click()
                except Exception as click_err:
                    logger.warning(f"点击群 [{canonical}] 失败（第{attempt+1}次）：{click_err}")
                    self._session_controls.pop(canonical, None)
                    continue
                time.sleep(wait_after_click)
                current = self._get_current_chat_name()
                if current != canonical:
                    time.sleep(1.0)  # 窗口切换可能未完成，稍等再确认一次
                    current = self._get_current_chat_name()
                if current == canonical:
                    logger.debug(f"已确认进入群 [{canonical}]")
                    return True
                logger.warning(f"点击 [{canonical}] 后实际打开的是 [{current}]，"
                               f"会话列表可能重排，重扫后重试（第{attempt+1}次）")
            self._session_controls.pop(canonical, None)
            time.sleep(0.5)
        logger.error(f"群 [{canonical}] 尝试 {attempts} 次仍无法确认进入，本轮跳过")
        return False

    # ==================== 主流程 ====================

    def check_groups(self) -> List[Dict[str, str]]:
        """
        只检查有未读消息的群。
        先扫描会话列表检测未读，再进入对应群读取消息。
        """
        if not self._wx:
            if not self._init_wx():
                return []

        # 只获取有未读的群
        unread_groups = self._get_unread_groups()

        # 微信不给"当前打开在聊天窗口里的会话"显示未读角标（当成已读），
        # 只按角标采集会让这个群被永久漏采，所以把当前打开的监控群也并入本轮。
        # 追加到末尾：它在本轮最后被读取，能覆盖"轮询期间一直开着"这段窗口。
        current = self._get_current_chat_name()
        current_group = self._match_group_name(current) if current else None
        logger.debug(f"当前打开会话: {current!r} → 监控群: {current_group!r}")
        if current_group and current_group not in unread_groups:
            unread_groups.append(current_group)
            logger.debug(f"当前打开会话 [{current_group}] 无未读角标，一并纳入本轮采集")

        if not unread_groups:
            self._navigate_to_chat_list()
            return []

        all_new_messages = []

        for group_name in unread_groups:
            try:
                # 进入群聊：点击会话并验证当前打开的就是该群
                # （会话列表会因新消息重排，直接点击可能打开别的群）
                if not self._enter_group(group_name, wait_after_click=0.8):
                    logger.error(f"群 [{group_name}] 无法确认进入，本轮跳过")
                    continue

                # 边翻边读
                all_msgs = self._collect_messages()

                if not all_msgs:
                    logger.debug(f"群 [{group_name}] 无可见消息，跳过")
                    continue

                new_count = 0
                for msg in all_msgs:
                    # 跳过系统消息（时间戳等）
                    attr = getattr(msg, 'attr', '') or ''
                    if attr == 'system':
                        continue

                    content = getattr(msg, 'content', '') or ''
                    sender = getattr(msg, 'sender', '') or ''
                    msg_type = getattr(msg, 'type', 'text') or 'text'

                    # 只处理文本消息；引用回复（quote 类型，content 为回复正文）也需采集
                    if msg_type not in ('text', 'quote'):
                        continue

                    # 筛选：消息须提到配置的机器人/客服昵称（不要求@符号，兼容全角＠/半角@/无符号），
                    # 且包含配置的业务关键字；两项都命中才采集
                    if not any(alias in content for alias in self.bot_aliases):
                        logger.debug(f"筛选跳过(未提及机器人昵称): [{group_name}] {sender}: {content[:80]}")
                        continue
                    if not any(kw in content for kw in self.keywords):
                        logger.debug(f"筛选跳过(未命中业务关键字): [{group_name}] {sender}: {content[:80]}")
                        continue

                    # 去重
                    if not self.is_new_message(content):
                        logger.debug(f"筛选跳过(已处理过): [{group_name}] {sender}: {content[:80]}")
                        continue

                    new_count += 1
                    all_new_messages.append({
                        'group': group_name,
                        'content': content,
                        'sender': sender,
                    })

                if new_count > 0:
                    logger.info(f"群 [{group_name}] 发现 {new_count} 条新消息")
                else:
                    logger.debug(f"群 [{group_name}] 无新消息")

            except Exception as e:
                logger.error(f"处理群 [{group_name}] 失败: {e}")
                continue

        self._save_state()
        # 回到会话列表页，确保未读角标能正常显示
        self._navigate_to_chat_list()
        return all_new_messages

    # ==================== 边翻边读 ====================

    def _collect_messages(self, max_pages: int = 5) -> list:
        """
        PageUp + 锚点验证 + PageDown 填缝：
        1. 读当前屏，记住顶部消息为锚点
        2. PageUp 翻页
        3. 检查锚点是否在新屏中出现（有重叠=无缝隙）
        4. 锚点丢失时，PageDown 回退一次填充缝隙
        """
        collected = []
        seen_contents = set()

        def _read_page():
            """读取当前屏，返回 (新增数, 当前屏key集合, 顶部key)"""
            msgs = self._wx.GetAllMessage()
            if not msgs:
                return 0, set(), None
            page_keys = set()
            added = 0
            top_key = None
            for msg in msgs:
                content = getattr(msg, 'content', '') or ''
                sender = getattr(msg, 'sender', '') or ''
                key = f"{sender}|{content}"
                if top_key is None:
                    top_key = key
                page_keys.add(key)
                if key not in seen_contents:
                    seen_contents.add(key)
                    collected.append(msg)
                    added += 1
            return added, page_keys, top_key

        # 第1步：读当前屏
        added0, _, _ = _read_page()

        # 第2步：翻页采集
        shell = self._get_scroll_shell()
        if shell:
            last_top_key = None
            # 重新读一次获取锚点
            _, _, last_top_key = _read_page()

            for i in range(max_pages):
                shell.SendKeys("{PGUP}")
                time.sleep(1.0)

                added, page_keys, new_top = _read_page()

                # 锚点验证：上一页的顶部消息是否在当前屏中
                if last_top_key and last_top_key in page_keys:
                    logger.debug(f"  翻页{i+1}: 重叠OK 新增{added}条")
                elif last_top_key and last_top_key not in page_keys:
                    # 锚点丢失 → PageDown 回退填缝
                    logger.debug(f"  翻页{i+1}: 锚点丢失，PageDown填缝")
                    shell.SendKeys("{PGDN}")
                    time.sleep(0.8)
                    added2, _, _ = _read_page()
                    added += added2
                    logger.debug(f"  填缝后新增{added2}条")

                if added == 0:
                    logger.debug(f"  翻页{i+1}: 无新内容，到顶")
                    break

                last_top_key = new_top

            # 第3步：滚回底部
            shell.SendKeys("^{END}")
            time.sleep(0.3)

        collected.reverse()
        return collected

    def _get_scroll_shell(self):
        """获取 WScript.Shell 对象并设置消息列表焦点"""
        try:
            from wxauto4.uia import uiautomation as uia
            import win32com.client
            main_wnd = uia.WindowControl(Name='微信')
            if not main_wnd.Exists(2):
                return None
            list_ctrl = main_wnd.ListControl(Name='消息')
            if not list_ctrl.Exists(2):
                list_ctrl = main_wnd.ListControl()
            if not list_ctrl.Exists(2):
                return None
            list_ctrl.SetFocus()
            time.sleep(0.1)
            return win32com.client.Dispatch("WScript.Shell")
        except Exception as e:
            logger.debug(f"获取滚动控制失败: {e}")
            return None

    def _navigate_to_chat_list(self):
        """回到会话列表，并将会话列表滚动到顶部，确保所有监听群可见
        （微信会话列表是虚拟列表，若列表停留在底部，上方的监控群不在控件树中会导致漏采）"""
        try:
            from wxauto4.uia import uiautomation as uia
            main_wnd = uia.WindowControl(Name='微信')
            if not main_wnd.Exists(2):
                return

            # 新版微信：点击导航栏的「微信」按钮回到会话列表
            wx_btn = main_wnd.ButtonControl(Name='微信', ClassName='mmui::XTabBarItem')
            if wx_btn.Exists(2):
                wx_btn.Click()
                time.sleep(0.5)

            # 会话列表滚动到顶部，避免列表停留在底部导致监控群不可见
            self._scroll_session_list_to_top(main_wnd)

        except Exception as e:
            logger.debug(f"回到会话列表失败: {e}")

    def _scroll_session_list_to_top(self, main_wnd):
        """将会话列表滚动到顶部（虚拟列表只渲染可见项，列表在底部时上方监控群不可见）"""
        try:
            # 方法1：UIA ScrollPattern 精确滚动到顶部
            target = main_wnd.ListControl(Name='会话')
            if not target.Exists(2):
                target = self._find_splitter(main_wnd)
                if not target:
                    return False

            try:
                sp = target.GetScrollPattern()
                if sp and sp.CurrentCanScrollVertically:
                    sp.SetScrollPercent(-1, 0)  # 水平保持不动，垂直滚到 0%（顶部）
                    time.sleep(0.3)
                    logger.debug("会话列表已滚动到顶部(ScrollPattern)")
                    return True
            except Exception:
                pass

            # 方法2：模拟鼠标滚轮向上滚动（微信列表跟随鼠标悬停位置滚动）
            rect = main_wnd.BoundingRectangle
            x = rect.left + max(150, (rect.right - rect.left) // 6)
            y = rect.top + min(400, (rect.bottom - rect.top) // 2)
            ctypes.windll.user32.SetCursorPos(x, y)
            time.sleep(0.2)
            for _ in range(60):
                ctypes.windll.user32.mouse_event(0x0800, 0, 0, 120, 0)  # WHEEL_DELTA=120，向上滚动
            time.sleep(0.3)
            logger.debug("会话列表已滚动到顶部(滚轮)")
            return True

        except Exception as e:
            logger.debug(f"滚动会话列表失败: {e}")
            return False

    def _find_splitter(self, main_wnd, max_depth=6):
        """递归查找 ClassName='mmui::XSplitterView' 的会话区域容器（替代不存在的 FindControl）"""
        def walk(ctrl, depth):
            if depth > max_depth:
                return None
            try:
                if (ctrl.ClassName or '') == 'mmui::XSplitterView':
                    return ctrl
                for ch in ctrl.GetChildren():
                    r = walk(ch, depth + 1)
                    if r:
                        return r
            except Exception:
                pass
            return None
        return walk(main_wnd, 0)

    def _find_and_click_control(self, ctrl, target_name, max_depth, current_depth):
        """递归查找并点击包含目标名称的控件"""
        if current_depth > max_depth:
            return False

        name = ctrl.Name or ''
        first_line = name.split('\n')[0].strip()
        if target_name in first_line:
            try:
                ctrl.Click()
                time.sleep(0.3)
                return True
            except Exception:
                pass

        try:
            for child in ctrl.GetChildren():
                if self._find_and_click_control(child, target_name, max_depth, current_depth + 1):
                    return True
        except Exception:
            pass

        return False

    def _find_chat_input(self, main_wnd):
        """查找聊天输入框控件：取所有 Edit 类控件中位置最靠下的一个（聊天输入框在窗口底部，
        顶部 y<100 的是搜索框 mmui::XValidatorTextEdit，需排除）"""
        try:
            from wxauto4.uia import uiautomation as uia
            candidates = []

            def walk(ctrl, depth):
                if depth > 12:
                    return
                try:
                    cn = ctrl.ClassName or ''
                    if ctrl.ControlType == uia.ControlType.EditControl or 'Edit' in cn or 'Input' in cn:
                        r = ctrl.BoundingRectangle
                        candidates.append((ctrl, r))
                    for ch in ctrl.GetChildren():
                        walk(ch, depth + 1)
                except Exception:
                    pass

            walk(main_wnd, 0)
            if not candidates:
                return None
            # 取位置最靠下的控件（排除顶部搜索框：聊天输入框 top 必大于 100）
            candidates.sort(key=lambda x: x[1].top, reverse=True)
            ctrl, r = candidates[0]
            if r.top < 100:
                return None  # 只有搜索框，说明不在聊天页
            return ctrl
        except Exception:
            return None

    def _reset_input_box(self) -> bool:
        """强制清空聊天输入框：点击输入框聚焦 + Ctrl+A + Delete，
        用于发送失败后清除残留的图片/文件引用，恢复发送能力"""
        try:
            from wxauto4.uia import uiautomation as uia
            main_wnd = uia.WindowControl(Name='微信')
            if not main_wnd.Exists(2):
                return False
            edit = self._find_chat_input(main_wnd)
            if not edit:
                logger.debug("未找到聊天输入框，跳过重置")
                return False
            edit.Click()
            time.sleep(0.5)
            edit.SendKeys('{Ctrl}a')
            time.sleep(0.3)
            edit.SendKeys('{DELETE}')
            time.sleep(0.3)
            logger.debug("聊天输入框已强制清空")
            return True
        except Exception as e:
            logger.debug(f"重置输入框失败: {e}")
            return False

    # ==================== 去重与状态管理 ====================

    def _message_hash(self, text: str) -> str:
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                raw = data.get('seen_hashes', [])
                if isinstance(raw, dict):
                    # 兼容旧版时间戳格式：只取哈希
                    self._seen_hashes = set(raw.keys())
                else:
                    self._seen_hashes = set(raw)
                logger.info(f"已加载 {len(self._seen_hashes)} 条处理记录")
            except Exception as e:
                logger.warning(f"加载状态文件失败: {e}")

    def _save_state(self):
        try:
            recent = list(self._seen_hashes)[-5000:]
            self._seen_hashes = set(recent)
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump({'seen_hashes': recent}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存状态文件失败: {e}")

    def is_new_message(self, text: str) -> bool:
        h = self._message_hash(text)
        if h in self._seen_hashes:
            return False
        self._seen_hashes.add(h)
        return True

    # ==================== 群内回复 ====================

    def send_reply_to_group(self, group_name: str, person_name: str, content: str, image_paths=None) -> bool:
        """
        在指定群聊中@某人并发送办理情况（支持多张图片+文字）

        Args:
            group_name: 群聊名称（G 列）
            person_name: 要@的人（需求人，H 列）
            content: 办理情况文字内容
            image_paths: 办理情况图片文件路径，单个 str 或 list（可选）
        
        Returns:
            bool: 是否发送成功
        """
        try:
            if not self._wx:
                if not self._init_wx():
                    logger.error("微信未连接，无法发送回复")
                    return False

            # 0. 取人名用于 @：
            #    有空格（含全角空格）→ 取最后一段；无空格 → 整个名字直接 @
            #    无空格但含半角连字符时，仅当末段是中文人名才取末段（避免拆坏"售后客服-小助手(9:30-18:30)"这类带时间格式的昵称）
            normalized = person_name.replace('\u3000', ' ').replace('\u2014', ' ').replace('\u2013', ' ')
            segments = [s.strip() for s in normalized.split(' ') if s.strip()]
            if len(segments) > 1:
                # 有空格（如"售后 张三" → "张三"）：取最后一段，
                # 最后一段太短时回退用去空格后的全名
                at_name = segments[-1] if len(segments[-1]) >= 2 else person_name.replace(' ', '').replace('\u3000', '').strip()
            else:
                name_no_space = person_name.strip()
                if '-' in name_no_space:
                    # 含半角连字符：仅当末段是中文/英文人名（长度>=2）才取末段
                    parts = [p.strip() for p in name_no_space.split('-') if p.strip()]
                    last = parts[-1] if parts else ''
                    if last and len(last) >= 2 and re.search(r'[\u4e00-\u9fffA-Za-z]', last):
                        at_name = last
                    else:
                        at_name = name_no_space
                else:
                    # 无空格无连字符（如"销售小助手"）：整个名字直接 @
                    at_name = name_no_space
            logger.debug(f"@人名: 原始={person_name!r} → 用于@={at_name!r}")
            person_name = at_name

            if not person_name:
                logger.error("人名称为空，无法@")
                return False

            # 1. 进入群聊：点击 + 验证当前会话（表格群名可能缺 emoji，内部自动匹配规范名）
            canonical = self._match_group_name(group_name) or group_name
            if not self._enter_group(canonical, wait_after_click=4.0):
                logger.error(f"群 [{group_name}] 无法确认进入，无法发送")
                return False

            # 进入成功后统一改用规范群名，后续发送/重试都基于它
            group_name = canonical

            # 2. 规范化图片路径（支持单个文件或多个文件），只保留真实存在的文件
            if isinstance(image_paths, str):
                image_paths = [image_paths] if os.path.exists(image_paths) else []
            elif isinstance(image_paths, (list, tuple)):
                image_paths = [p for p in image_paths if p and os.path.exists(p)]
            else:
                image_paths = []
            has_image = bool(image_paths)

            # 3. 构建消息内容（文字部分）
            msg_content = str(content) if content else ""

            # 3. 发送图片 + 文字（含 @），带重试
            max_retries = 2
            for attempt in range(max_retries):
                try:
                    time.sleep(1.5)  # 发送前等待：确保聊天窗口完全就绪、焦点在输入框

                    # 3a. 先发送图片文件（如果有本地图片，多张一次发送）
                    if has_image:
                        logger.debug(f"发送图片：{image_paths}")
                        try:
                            self._wx.SendFiles(image_paths, who=group_name)
                            # 多图发送后等待更久，确保全部发送完成、界面恢复
                            time.sleep(3.0 + len(image_paths))
                            logger.info(f"已发送 {len(image_paths)} 张图片到群 [{group_name}]")
                        except Exception as img_err:
                            # 图片发送失败：清空输入框残留并降级为仅发文字，不阻塞@通知
                            logger.warning(f"图片发送失败，清空输入框后降级为仅文字：{img_err}")
                            self._reset_input_box()
                            has_image = False

                    # 3b. 发送文字消息 + @
                    if has_image:
                        send_text = msg_content if msg_content else "[图片]"
                    else:
                        send_text = msg_content if msg_content else ""
                    if send_text or person_name:
                        self._wx.SendMsg(send_text, who=group_name, at=person_name)
                        logger.info(f"已回复群 [{group_name}] @{person_name}: {send_text[:50]}")

                    return True

                except Exception as send_err:
                    logger.warning(f"第{attempt+1}次发送失败：{send_err}")
                    if attempt < max_retries - 1:
                        logger.debug(f"重置输入框并等待5秒后重试...")
                        self._reset_input_box()  # 先清空输入框残留，避免"中毒"连锁失败
                        time.sleep(5.0)
                        # 重新确认进入目标群再重发，避免在错误窗口发出消息
                        if not self._enter_group(group_name, wait_after_click=4.0):
                            raise send_err
                    else:
                        raise send_err

            return False

        except Exception as e:
            logger.error(f"回复群 [{group_name}] 失败：{e}")
            return False
