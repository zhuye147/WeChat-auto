"""
消息解析模块
从微信消息文本中提取结构化的业务办理信息
"""
import re
import logging
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

# 支持中英文冒号
_FIELD_NAMES = ['学员编号', '姓名', '班级编号', '订单号', '业务办理', '备注']

# 匹配 "字段名：" 及其在文本中的位置
_FIELD_LABEL_PATTERN = re.compile(
    r'(' + '|'.join(_FIELD_NAMES) + r')\s*[:：]'
)

# 用于检测消息块中是否包含关键字段（至少要有学员编号或姓名）
_KEY_FIELDS = {'学员编号', '姓名'}


def extract_sender(text: str) -> str:
    """
    从消息文本中提取发送者名称。

    WeChat UIA 的消息格式通常为:
        发送人名称
        学员编号: xxx
        姓名: xxx
        ...

    第一行通常就是发送人名称（如果不是已知字段名或时间戳格式）。

    Args:
        text: 消息文本

    Returns:
        发送者名称，未识别到时返回空字符串
    """
    lines = text.strip().split('\n')
    if not lines:
        return ''

    first_line = lines[0].strip()
    if not first_line:
        return ''

    # 如果第一行是已知字段名，则不是发送人
    known_prefixes = {'学员编号', '姓名', '班级编号', '订单号', '业务办理', '备注', '@'}
    if any(first_line.startswith(p) for p in known_prefixes):
        return ''

    # 如果第一行是时间戳格式（如 2026-07-23 或 2026/07/23），尝试取第二行
    if re.match(r'^\d{4}[-/]\d{2}[-/]\d{2}', first_line):
        if len(lines) > 1:
            second_line = lines[1].strip()
            if second_line and not any(second_line.startswith(p) for p in known_prefixes):
                return second_line
        return ''

    return first_line


def parse_message(text: str) -> Optional[Dict[str, str]]:
    """
    解析单条消息文本，提取业务办理信息。
    采用字段区间截取法：找到每个字段标签的位置，截取相邻标签之间的内容作为值。
    这样即使某个字段为空，也不会把下一行的字段名误当成值。
    """
    text = text.strip()
    if not text:
        return None

    # 找出所有字段标签及其在文本中的位置
    matches = list(_FIELD_LABEL_PATTERN.finditer(text))
    if not matches:
        return None

    # 按位置排序，逐个截取字段值
    fields = {}
    for idx, m in enumerate(matches):
        field_name = m.group(1)
        value_start = m.end()  # 冒号后的第一个字符位置

        # 值的结束位置 = 下一个字段标签的起始位置，或者是文本末尾
        if idx + 1 < len(matches):
            value_end = matches[idx + 1].start()
        else:
            value_end = len(text)

        value = text[value_start:value_end].strip()
        fields[field_name] = value

    # 至少包含一个关键字段才认为是有效消息
    if not _KEY_FIELDS & set(fields.keys()):
        return None

    result = {name: fields.get(name, '') for name in _FIELD_NAMES}

    logger.debug(f"解析成功: {result}")
    return result


def extract_messages_from_text(text: str) -> List[Dict[str, str]]:
    """
    从大段文本中提取所有业务办理消息。
    
    微信复制的消息可能包含多条消息，此方法会尝试按消息分隔符拆分，
    然后逐条解析。
    
    Args:
        text: 从微信复制的大段文本
        
    Returns:
        解析成功的消息列表
    """
    results = []
    
    # 先尝试整体解析
    parsed = parse_message(text)
    if parsed:
        results.append(parsed)
        return results
    
    # 按常见分隔符拆分，尝试逐段解析
    # 微信复制的消息之间可能有空行或时间戳分隔
    segments = re.split(r'\n\s*\n|\n(?=\d{4}[-/])', text)
    
    for segment in segments:
        parsed = parse_message(segment)
        if parsed:
            results.append(parsed)
    
    # 如果拆分后也没找到，尝试按行滑动窗口提取
    if not results:
        lines = text.split('\n')
        current_block = []
        for line in lines:
            line = line.strip()
            if not line:
                # 空行可能是消息分界
                if current_block:
                    parsed = parse_message('\n'.join(current_block))
                    if parsed:
                        results.append(parsed)
                    current_block = []
            else:
                current_block.append(line)
        
        # 处理最后一个块
        if current_block:
            parsed = parse_message('\n'.join(current_block))
            if parsed:
                results.append(parsed)
    
    if results:
        logger.info(f"从文本中提取到 {len(results)} 条业务消息")
    else:
        logger.debug("文本中未找到业务办理消息")
    
    return results
