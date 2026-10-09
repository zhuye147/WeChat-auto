"""
客服数据监听 - 主程序
定时监听微信群消息，解析业务办理信息，自动写入飞书多维表格
"""
import os
import sys
import types
import time
import signal
import json
import logging
import logging.handlers
import yaml
from datetime import datetime

# Mock win32ui（wxauto4 导入了但未实际使用）
sys.modules['win32ui'] = types.ModuleType('win32ui')

from msg_parser import parse_message
from wechat_uia import WeChatUIA
from feishu_api import FeishuAPI, COL_MAP, _is_yes

# ==================== 全局配置 ====================

CONFIG_FILE = "config.yaml"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 优雅退出
_running = True

def _signal_handler(sig, frame):
    global _running
    print("\n收到退出信号，正在停止...")
    _running = False

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ==================== 工具函数 ====================

def load_config() -> dict:
    """加载配置文件"""
    config_path = os.path.join(BASE_DIR, CONFIG_FILE)
    if not os.path.exists(config_path):
        print(f"错误: 配置文件不存在: {config_path}")
        print("请复制 config.yaml 并填写你的配置信息")
        sys.exit(1)
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 验证必要配置
    feishu_cfg = config.get('feishu', {})
    if not feishu_cfg.get('app_id') or 'xxx' in feishu_cfg.get('app_id', 'xxx'):
        print("错误: 请在 config.yaml 中填写飞书应用的 app_id 和 app_secret")
        sys.exit(1)
    
    if not feishu_cfg.get('spreadsheet_token') or 'xxx' in feishu_cfg.get('spreadsheet_token', 'xxx'):
        print("错误: 请在 config.yaml 中填写电子表格的 spreadsheet_token")
        sys.exit(1)
    
    wechat_cfg = config.get('wechat', {})
    if not wechat_cfg.get('groups'):
        print("错误: 请在 config.yaml 中配置需要监听的微信群名称")
        sys.exit(1)
    
    return config


def setup_logging(config: dict):
    """配置日志"""
    # 关闭 wxauto4 自身的文件日志（wxauto_logs），避免日志无限增长
    try:
        from wxauto4.param import WxParam
        WxParam.ENABLE_FILE_LOGGER = False
    except Exception:
        pass

    log_cfg = config.get('logging', {})
    level = getattr(logging, log_cfg.get('level', 'INFO').upper(), logging.INFO)
    log_file = os.path.join(BASE_DIR, log_cfg.get('file', 'monitor.log'))
    
    # 日志格式
    fmt = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    datefmt = '%Y-%m-%d %H:%M:%S'
    formatter = logging.Formatter(fmt, datefmt=datefmt)
    
    # 配置 root logger（显式添加 handler，避免被 wxauto4 覆盖）
    root = logging.getLogger()
    root.setLevel(level)
    # 清除已有 handler
    root.handlers.clear()
    
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    root.addHandler(sh)
    
    # 日志文件轮转：单个超过 20MB 自动归档，最多保留 3 个备份（约 80MB 封顶）
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=20 * 1024 * 1024, backupCount=3, encoding='utf-8'
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)

    # 抑制噪音日志
    logging.getLogger('comtypes').setLevel(logging.WARNING)
    logging.getLogger('wxauto4').setLevel(logging.WARNING)


def print_banner():
    """打印启动横幅"""
    print("=" * 55)
    print("       客服数据监听 - 微信群消息自动采集工具")
    print("=" * 55)
    print(f"  启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  配置文件: {CONFIG_FILE}")
    print(f"  按 Ctrl+C 停止程序")
    print("=" * 55)


# ==================== 单实例锁 ====================

LOCK_FILE = os.path.join(BASE_DIR, "monitor.lock")


def _lock_owner_alive(pid: int) -> bool:
    """检查锁文件中的 PID 是否仍在运行"""
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:
        return False


def acquire_lock() -> bool:
    """获取单实例锁，防止重复启动导致两个实例同时写表格/操作微信互相冲突"""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if _lock_owner_alive(int(data.get('pid', 0))):
                print("检测到已有实例在运行，本实例退出")
                return False
        except Exception:
            pass
    with open(LOCK_FILE, 'w', encoding='utf-8') as f:
        json.dump({'pid': os.getpid()}, f)
    return True


def release_lock():
    """释放单实例锁（进程被杀时残留的锁文件会在下次启动时自动接管）"""
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


# ==================== 主流程 ====================

def _monitor_main():
    
    global _running
    
    # 加载配置
    config = load_config()
    setup_logging(config)
    logger = logging.getLogger("main")
    
    print_banner()
    
    # 初始化飞书 API
    feishu_cfg = config['feishu']
    feishu = FeishuAPI(
        app_id=feishu_cfg['app_id'],
        app_secret=feishu_cfg['app_secret'],
        spreadsheet_token=feishu_cfg['spreadsheet_token'],
        sheet_id=feishu_cfg.get('sheet_id')
    )
    
    # 测试飞书连接
    logger.info("正在测试飞书连接...")
    if not feishu.test_connection():
        logger.error("飞书连接失败，请检查配置和网络")
        logger.error("提示: 请确保:")
        logger.error("  1. app_id 和 app_secret 正确")
        logger.error("  2. 电子表格已创建并授权给应用")
        logger.error("  3. 网络可以访问 open.feishu.cn")
        sys.exit(1)
    logger.info("飞书连接正常 ✓")
    
    # 初始化微信监听
    wechat_cfg = config['wechat']
    groups = wechat_cfg['groups']
    check_interval = wechat_cfg.get('check_interval', 60)
    
    monitor = WeChatUIA(
        groups=groups,
        state_file=os.path.join(BASE_DIR, "processed_state.json"),
        bot_aliases=wechat_cfg.get('bot_aliases'),
        keywords=wechat_cfg.get('keywords')
    )
    if not wechat_cfg.get('bot_aliases'):
        logger.warning("config.yaml 未配置 wechat.bot_aliases，将使用内置默认筛选词，可能采集不到消息")
    if not wechat_cfg.get('keywords'):
        logger.warning("config.yaml 未配置 wechat.keywords，将使用内置默认筛选词，可能采集不到消息")
    
    # 检查微信是否运行
    if not monitor.check_wechat_running():
        logger.warning("未检测到微信PC客户端，请先登录微信")
        logger.warning("程序将继续运行，等待微信启动...")
    
    logger.info(f"监听群列表: {groups}")
    logger.info(f"轮询间隔: {check_interval} 秒")
    logger.info("开始监听...")
    print("-" * 55)

    # 回复检查与抓取消息交替进行：每抓取 5 次后执行 1 次回复检查（同一线程内同步执行，
    # 避免两个任务同时操作微信界面导致切群冲突）
    reply_counter = 0
    REPLY_EVERY_N_ROUNDS = 5  # 每 5 次抓取后执行 1 次回复检查
    logger.info(f"回复检查：每 {REPLY_EVERY_N_ROUNDS} 轮抓取后执行 1 次")
    
    total_processed = 0
    
    while _running:
        try:
            # 检查微信是否在运行
            if not monitor.check_wechat_running():
                logger.debug("微信未运行，等待中...")
                time.sleep(check_interval)
                continue
            
            # 检查各群新消息
            new_messages = monitor.check_groups()
            
            if new_messages:
                all_records = []
                
                for msg in new_messages:
                    group_name = msg['group']
                    text = msg['content']
                    sender = msg.get('sender', '')
                    
                    # 解析每条消息
                    parsed = parse_message(text)
                    if parsed:
                        parsed['群聊'] = group_name
                        parsed['发送人'] = sender
                        logger.debug(f"群 [{group_name}] 解析到业务消息: {parsed}")
                        all_records.append(parsed)
                
                if all_records:
                    # 写入飞书
                    row_results = feishu.write_records_batch(all_records)
                    success = sum(1 for r in row_results if r is not None)
                    total_processed += success
                    logger.info(f"本轮写入 {success}/{len(all_records)} 条，"
                               f"累计处理 {total_processed} 条")
                else:
                    logger.debug("新消息中未筛选出业务办理信息")

            # 每抓取 REPLY_EVERY_N_ROUNDS 次后，同步执行一次回复检查（与抓取交替，不冲突）
            reply_counter += 1
            if reply_counter >= REPLY_EVERY_N_ROUNDS:
                reply_counter = 0
                try:
                    check_and_send_replies(feishu, monitor)
                except Exception as e:
                    logger.error(f"回复检查异常：{e}", exc_info=True)

            # 等待下一轮
            logger.debug(f"等待 {check_interval} 秒后进行下一轮检查...")
            for _ in range(check_interval):
                if not _running:
                    break
                time.sleep(1)
                
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"主循环异常: {e}", exc_info=True)
            time.sleep(10)  # 异常后等待 10 秒再继续
    
    # 退出
    logger.info(f"程序停止，累计处理 {total_processed} 条记录")
    print("\n程序已退出。")


def check_and_send_replies(feishu, monitor):
    """
    检查并发送群内回复（单次执行）

    由主循环每抓取 5 轮后调用一次，与抓取消息在同一线程内交替执行，
    确保回复操作和抓取操作不会同时进行、互不冲突。

    Args:
        feishu: FeishuAPI 实例
        monitor: WeChatUIA 实例
    """
    logger = logging.getLogger("main")
    try:
        logger.info("开始检查待回复记录...")

        # 读取所有记录
        records = feishu.read_all_records()

        # 调试：打印所有"是否办理完成=是"的记录
        for r in records:
            if _is_yes(r.get('是否办理完成')):
                logger.debug(f"第{r.get('_row')}行: 办理完成=是, 群内回复={r.get('是否群内回复')!r}, 群聊={r.get('群聊')!r}, 需求人={r.get('需求人')!r}")

        # 筛选：是否办理完成=是 且 是否群内回复!=是（容忍单元格空白/None）
        pending_replies = [
            r for r in records
            if _is_yes(r.get('是否办理完成'))
            and str(r.get('是否群内回复') or '').strip() != '是'
        ]

        if not pending_replies:
            logger.info("无待回复记录")
            return

        logger.info(f"发现 {len(pending_replies)} 条待回复记录")

        for record in pending_replies:
            if not _running:
                return

            # 发送前重新定位：快照行号在表格变动后会漂移，是回错行的根源之一
            current = feishu.locate_pending_record(record)
            if current is None:
                logger.warning(f"跳过记录（原第{record.get('_row')}行，姓名={record.get('姓名')!r}）")
                continue
            record = current
            row = record['_row']

            group_name = record.get('群聊', '')
            person_name = record.get('需求人', '')

            if not group_name or not person_name:
                logger.warning(f"第{row}行缺少群聊或发送人信息，跳过")
                continue

            # 处理内容：办理情况 / 办理情况2 两列都可能为空、图片或文字，合并提取
            image_paths = []
            file_tokens = []
            text_parts = []
            for key in ('办理情况', '办理情况2'):
                col_value = record.get(key, '')
                if not col_value:
                    continue
                tokens, text = feishu.extract_images_and_text(col_value)
                file_tokens.extend(tokens)
                if text:
                    text_parts.append(text)
            content = '\n'.join(text_parts)

            if file_tokens:
                temp_dir = os.path.join(BASE_DIR, 'temp_images')
                for token in file_tokens:
                    image_path = feishu.download_image(token, temp_dir)
                    if image_path:
                        image_paths.append(image_path)
                        logger.info(f"第{row}行图片已下载：{image_path}")
                    else:
                        logger.warning(f"第{row}行图片下载失败：{token[:20]}...")

            # 发送回复
            success = monitor.send_reply_to_group(
                group_name=group_name,
                person_name=person_name,
                content=content,
                image_paths=image_paths
            )

            if success:
                # 发送成功后重新定位行号再更新（发送耗时期间表格可能变动）
                updated = feishu.locate_pending_record(record)
                if updated is None:
                    logger.error(f"第{row}行已回复，但表格中已无法定位该记录，"
                                 f"请人工核对并更新'是否群内回复'列")
                else:
                    feishu.update_cell(updated['_row'], COL_MAP['是否群内回复'], '是')
                    logger.info(f"第{updated['_row']}行已回复并更新")
            else:
                logger.error(f"第{row}行回复失败")

            time.sleep(2)  # 避免操作过快

    except Exception as e:
        logger.error(f"检查回复异常：{e}", exc_info=True)


def main():
    """入口：获取单实例锁后运行监听主流程"""
    if not acquire_lock():
        return
    try:
        _monitor_main()
    finally:
        release_lock()


if __name__ == "__main__":
    main()
