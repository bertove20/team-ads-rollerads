"""Mengelola bot-bot Telegram milik para agent dan topic di grup."""
import logging
import os

from telegram import Bot, InlineKeyboardMarkup, InputFile, LinkPreviewOptions, Message, ReplyParameters
from telegram.error import TelegramError

import config
import storage
from agents import AGENTS, LEADER

log = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 4000


def topic_id(topic: str) -> int | None:
    """ID thread topic, atau None (topic General) jika /setup belum dijalankan."""
    return storage.get("topics", {}).get(topic)


def _split(text: str) -> list[str]:
    chunks = []
    while len(text) > MAX_MESSAGE_LEN:
        cut = text.rfind("\n", 0, MAX_MESSAGE_LEN)
        cut = cut if cut > 0 else MAX_MESSAGE_LEN
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


class Team:
    def __init__(self, leader_bot: Bot | None):
        """`leader_bot=None` berarti mode dashboard saja: pesan hanya dicatat, tidak dikirim ke Telegram."""
        self.bots: dict[str, Bot] = {LEADER: leader_bot} if leader_bot else {}
        self.agent_by_username: dict[str, str] = {}
        self._extra_bots: list[Bot] = []

    @property
    def online(self) -> bool:
        return LEADER in self.bots and bool(config.GROUP_ID)

    async def start(self) -> None:
        if LEADER not in self.bots:
            return
        for key, agent in AGENTS.items():
            token = os.getenv(agent.token_env, "").strip()
            if key == LEADER or not token:
                continue
            bot = Bot(token)
            try:
                await bot.initialize()
            except TelegramError as e:
                log.error("Bot %s gagal dijalankan: %s", agent.name, e)
                continue
            self.bots[key] = bot
            self._extra_bots.append(bot)
        for key, bot in self.bots.items():
            self.agent_by_username[bot.username.lower()] = key
        log.info("Agent dengan bot sendiri: %s", ", ".join(self.bots))

    async def stop(self) -> None:
        for bot in self._extra_bots:
            await bot.shutdown()

    @property
    def leader(self) -> Bot:
        return self.bots[LEADER]

    async def send(
        self,
        agent_key: str,
        topic: str,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        reply_to: int | None = None,
        via_leader: bool = False,
    ) -> Message | None:
        """Kirim pesan sebagai agent ke topic tertentu. Semua pesan juga dicatat untuk dashboard.

        Pesan bertombol harus dikirim lewat bot leader, karena hanya bot leader yang
        menerima klik tombol.
        """
        agent = AGENTS[agent_key]
        if not self.online:
            storage.log_chat(None, agent.label, text, topic)
            return None
        use_leader = via_leader or agent_key not in self.bots
        bot = self.leader if use_leader else self.bots[agent_key]
        body = f"{agent.label}:\n{text}" if use_leader and agent_key != LEADER else text
        thread_id = topic_id(topic)

        message = None
        chunks = _split(body)
        try:
            for i, chunk in enumerate(chunks):
                message = await bot.send_message(
                    chat_id=config.GROUP_ID,
                    text=chunk,
                    message_thread_id=thread_id,
                    reply_markup=reply_markup if i == len(chunks) - 1 else None,
                    reply_parameters=(
                        ReplyParameters(reply_to, allow_sending_without_reply=True)
                        if reply_to and i == 0
                        else None
                    ),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
        except TelegramError as e:
            log.error("Gagal kirim pesan %s ke topic %s: %s", agent.name, topic, e)
            if not use_leader:  # coba lagi lewat bot leader
                return await self.send(
                    agent_key, topic, text, reply_markup=reply_markup, reply_to=reply_to, via_leader=True
                )
            message = None
        storage.log_chat(thread_id, agent.label, text, topic)
        return message

    async def send_file(self, agent_key: str, topic: str, filename: str, content: str, caption: str = "") -> None:
        """Kirim file teks (mis. script tracking) sebagai agent. Isi file tidak dipecah seperti pesan biasa."""
        agent = AGENTS[agent_key]
        storage.log_chat(topic_id(topic) if self.online else None, agent.label, f"📎 {filename}\n{caption}", topic)
        if not self.online:
            return
        use_leader = agent_key not in self.bots
        bot = self.leader if use_leader else self.bots[agent_key]
        if use_leader and agent_key != LEADER:
            caption = f"{agent.label}:\n{caption}"
        try:
            await bot.send_document(
                chat_id=config.GROUP_ID,
                document=InputFile(content.encode("utf-8"), filename=filename),
                caption=caption[:1000] or None,
                message_thread_id=topic_id(topic),
            )
        except TelegramError as e:
            log.error("Gagal kirim file %s dari %s: %s", filename, agent.name, e)

    async def setup_topics(self) -> list[str]:
        """Buat topic yang belum ada. Bot leader harus admin dengan izin 'Manage Topics'."""
        topics = storage.get("topics", {})
        created = []
        for key, name in config.TOPICS.items():
            if key in topics:
                continue
            topic = await self.leader.create_forum_topic(chat_id=config.GROUP_ID, name=name)
            topics[key] = topic.message_thread_id
            storage.put("topics", topics)
            created.append(name)
        return created
