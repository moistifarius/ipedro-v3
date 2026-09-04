"""Shared bot runtime: holds references that handlers need."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot

from ipedro.config import Settings
from ipedro.db.pool import Database
from ipedro.db.repositories import ChatRepo, CommandLogRepo, UserRepo
from ipedro.duckhunt.service import DuckhuntService
from ipedro.memory.store import MemoryStore
from ipedro.openai_client import OpenAIClient


@dataclass
class Runtime:
    settings: Settings
    bot: Bot
    db: Database
    openai: OpenAIClient
    memory: MemoryStore
    duckhunt: DuckhuntService
    chats: ChatRepo
    users: UserRepo
    command_log: CommandLogRepo
    pgvector_available: bool
