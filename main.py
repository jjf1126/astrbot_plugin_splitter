import re
import json
import asyncio
from typing import List

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Plain, BaseMessageComponent

@register("astrbot_plugin_splitter", "YourName", "LLM 输出自动分段发送插件", "1.0.1")
class MessageSplitterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        拦截消息发送，进行历史记录保存、文本清理、分段发送。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 1. 获取配置
        split_pattern = self.config.get("split_regex", r"\n\s*\n")
        clean_pattern = self.config.get("clean_regex", "")
        delay = self.config.get("delay", 1.0)

        # 2. 预处理：构建完整文本用于历史记录，并执行清理
        full_text_for_history = ""
        cleaned_chain = []

        for component in result.chain:
            if isinstance(component, Plain):
                text = component.text
                full_text_for_history += text # 记录原始文本到历史（或者记录清理后的，视需求而定，这里记录原始的）
                
                # 执行清理逻辑
                if clean_pattern:
                    text = re.sub(clean_pattern, "", text)
                
                # 如果清理后还有内容，加入待处理链
                if text:
                    cleaned_chain.append(Plain(text))
            else:
                # 非文本组件直接保留
                cleaned_chain.append(component)

        # 3. 执行分段逻辑
        segments = self.split_chain(cleaned_chain, split_pattern)

        # 如果没有分段（长度为1），且清理并未改变链条结构，则不做干预，让原流程继续
        # 但如果进行了清理（cleaned_chain != result.chain），即使没分段也要接管发送
        is_cleaned = (clean_pattern != "")
        
        if len(segments) <= 1 and not is_cleaned:
            return

        logger.info(f"[Splitter] 消息已处理: 分段数={len(segments)}, 是否清理={is_cleaned}")

        # 4. 【关键步骤】手动保存 LLM 回复到对话历史
        # 因为我们要清空原消息链，AstrBot 核心可能无法正确记录历史，或者记录为空。
        # 我们需要手动将 Assistant 的回复追加到当前 Conversation 中。
        await self.save_history(event, full_text_for_history)

        # 5. 发送分段消息
        for i, segment_chain in enumerate(segments):
            if not segment_chain:
                continue
            
            try:
                # 修复之前的 'list' object has no attribute 'chain' 错误
                mc = MessageChain()
                mc.chain = segment_chain
                
                await self.context.send_message(event.unified_msg_origin, mc)
                
                if i < len(segments) - 1:
                    await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"[Splitter] 发送分段消息失败: {e}")

        # 6. 【关键步骤】阻止源消息发送
        # 使用 chain.clear() 确保没有任何内容传递给适配器，解决“重复发送”和“发送两次”的问题
        result.chain.clear()
        # 显式停止事件传播，防止后续可能的钩子触发
        event.stop_event()

    async def save_history(self, event: AstrMessageEvent, content: str):
        """
        手动将 Assistant 的回复写入对话历史
        """
        try:
            conv_mgr = self.context.conversation_manager
            # 获取当前对话 ID
            cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)
            if not cid:
                return

            # 获取对话对象
            conversation = await conv_mgr.get_conversation(event.unified_msg_origin, cid)
            if not conversation:
                return

            # 解析历史记录 (通常是 JSON 字符串)
            history_list = []
            if conversation.history:
                try:
                    history_list = json.loads(conversation.history)
                except json.JSONDecodeError:
                    history_list = []
            
            # 追加 Assistant 消息
            # 注意：这里假设上一条 User 消息已经被 AstrBot 核心逻辑添加进去了。
            # 通常 User 消息在处理开始时添加，Assistant 在处理结束时添加。
            # 我们拦截的是结束阶段，所以追加 Assistant 消息是安全的。
            history_list.append({
                "role": "assistant",
                "content": content
            })

            # 更新数据库
            await conv_mgr.update_conversation(
                unified_msg_origin=event.unified_msg_origin,
                conversation_id=cid,
                history=history_list
            )
        except Exception as e:
            logger.error(f"[Splitter] 手动保存历史记录失败: {e}")

    def split_chain(self, chain: List[BaseMessageComponent], pattern: str) -> List[List[BaseMessageComponent]]:
        segments = []
        current_buffer = []

        for component in chain:
            if isinstance(component, Plain):
                text = component.text
                # 使用正则分割
                parts = re.split(pattern, text)

                if len(parts) == 1:
                    current_buffer.append(component)
                else:
                    if parts[0]:
                        current_buffer.append(Plain(parts[0]))
                    
                    if current_buffer:
                        segments.append(current_buffer)
                        current_buffer = []

                    for mid_part in parts[1:-1]:
                        if mid_part:
                            segments.append([Plain(mid_part)])
                    
                    if parts[-1]:
                        current_buffer.append(Plain(parts[-1]))
            else:
                current_buffer.append(component)

        if current_buffer:
            segments.append(current_buffer)

        return segments
