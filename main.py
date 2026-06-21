"""
astrbot_plugin_sleep_sense v1.4.0
让 Bot 拥有真实睡眠感知：睡觉、慵懒、熬夜、补觉、吵醒、睡眠周期、做梦
新增：晚安报告图片、早安通知、/对方 指令、作息重叠提示、跨时区补觉
作者: 夕小柠
"""

import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    _HAS_PATH_API = True
except Exception:
    _HAS_PATH_API = False

# ── 跨插件联动总线 ────────────────────────────────────────────────────────
# 协议见 astrbot_plugin_air_sense 的 README：纯文件约定，写一个标准格式的 json，
# 任何遵循这个协议读取的插件（比如读空气插件）都能感知到本插件的睡眠状态，
# 不需要互相 import，没装那个插件也完全不影响本插件正常运行。
def _bus_dir() -> Path:
    base = Path(get_astrbot_data_path()) if _HAS_PATH_API else Path("data")
    d = base / "plugin_data" / "_companion_bus"
    d.mkdir(parents=True, exist_ok=True)
    return d

# ── 数据目录 ──────────────────────────────────────────────────────────────
DATA_DIR = Path("data/sleep_sense")
STATE_PATH     = DATA_DIR / "state.json"
ALARMS_PATH    = DATA_DIR / "alarms.json"
DREAMS_PATH    = DATA_DIR / "dreams.json"
DAILY_LOG_PATH = DATA_DIR / "daily_log.json"
LOG_PATH       = DATA_DIR / "logs" / "sleep.log"
STATS_DIR      = DATA_DIR / "stats"
ASSETS_DIR     = Path(__file__).parent / "assets"
FONT_REG       = str(ASSETS_DIR / "fonts" / "NotoSansSC-Regular.ttf")
FONT_BOLD      = str(ASSETS_DIR / "fonts" / "NotoSansSC-Bold.ttf")


# ── 状态常量 ──────────────────────────────────────────────────────────────
class S:
    AWAKE    = "awake"
    LAZY     = "lazy"
    SLEEPING = "sleeping"
    NAPPING  = "napping"
    OVERTIME = "overtime"


# ── 平台兼容性工具 ────────────────────────────────────────────────────────
def _is_group(event: AstrMessageEvent) -> bool:
    try:
        msg = event.message_obj
        if hasattr(msg, "group_id") and msg.group_id:
            return True
        if hasattr(msg, "is_group"):
            return bool(msg.is_group)
        return "group" in str(getattr(msg, "type", "")).lower()
    except Exception:
        return False


def _is_at_me(event: AstrMessageEvent) -> bool:
    # 先尝试拿到机器人自己的QQ号，用于raw_message精确验证
    self_id = None
    for getter in ("get_self_id", "self_id"):
        try:
            val = getattr(event, getter, None)
            self_id = val() if callable(val) else val
            if self_id:
                break
        except Exception:
            continue
    if not self_id:
        try:
            self_id = getattr(event.message_obj, "self_id", None)
        except Exception:
            self_id = None

    try:
        raw = str(getattr(event.message_obj, "raw_message", "") or "")
    except Exception:
        raw = ""

    # 如果能拿到self_id，优先用raw_message精确判断（最可靠）
    # NapCat/aiocqhttp下 event.is_at_me() 有时对所有群消息都返回True，不可信
    if self_id and raw:
        return f"[CQ:at,qq={self_id}]" in raw or f"[CQ:at:qq={self_id}" in raw

    # 拿不到self_id才fallback到is_at_me()
    try:
        if hasattr(event, "is_at_me") and callable(event.is_at_me):
            return bool(event.is_at_me())
    except Exception:
        pass

    # 最后兜底：既没有self_id也没有is_at_me，保守返回False
    return False


# ── 插件主类 ──────────────────────────────────────────────────────────────
@register("sleep_sense", "夕小柠", "让 Bot 拥有真实睡眠感知", "1.2.0")
class SleepSensePlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config   # AstrBotConfig 是 dict 子类，用 self.cfg.get() 读，self.cfg['key'] = v + save_config() 写

        for d in [DATA_DIR / "logs", STATS_DIR, DATA_DIR / "overtime" / "history"]:
            d.mkdir(parents=True, exist_ok=True)

        self.state  = self._load_state()
        self._publish_bus_state()  # 启动时立即同步一次状态，不用等下次状态切换
        self.alarms: list = self._load_json(ALARMS_PATH, [])
        self.dreams: list = self._load_json(DREAMS_PATH, [])
        self.daily_log: list = self._load_json(DAILY_LOG_PATH, [])

        self._msg_counters: dict[str, int]   = {}
        self._woken_msg_count: dict[str, int] = {}
        self._last_admin_ts: float           = time.time()
        self._last_admin_text: str           = ""
        self._last_others_ts: dict[str, float] = {}
        self._pre_sleep_ctx: list[str]       = []
        self._dream_generated_tonight: bool  = False
        self._cycle_plan: list = []
        self._lock = asyncio.Lock()
        self._log_level: str = self.cfg.get("log_level", "info")

        asyncio.create_task(self._scheduler_loop())
        asyncio.create_task(self._alarm_loop())
        logger.info(f"[sleep_sense] 插件已启动，状态: {self.state['sleep_state']}")

        # ── WebUI 配置 API ──────────────────────────────────────────
        context.register_web_api(
            "/astrbot_plugin_sleep_sense/config",
            self._api_get_config,
            ["GET"],
            "获取配置",
        )
        context.register_web_api(
            "/astrbot_plugin_sleep_sense/config",
            self._api_post_config,
            ["POST"],
            "保存配置",
        )
        context.register_web_api(
            "/astrbot_plugin_sleep_sense/status",
            self._api_get_status,
            ["GET"],
            "获取当前睡眠状态",
        )
        context.register_web_api(
            "/astrbot_plugin_sleep_sense/sleep_history",
            self._api_get_sleep_history,
            ["GET"],
            "获取最近N天睡眠时长历史",
        )
        context.register_web_api(
            "/astrbot_plugin_sleep_sense/daily_log",
            self._api_get_daily_log,
            ["GET"],
            "获取每日完整睡眠日志（含周期轨迹、评分、梦境）",
        )
        context.register_web_api(
            "/astrbot_plugin_sleep_sense/dreams",
            self._api_get_dreams,
            ["GET"],
            "获取梦境记录列表",
        )
        context.register_web_api(
            "/astrbot_plugin_sleep_sense/alarms",
            self._api_get_alarms,
            ["GET"],
            "获取当前临时闹钟列表",
        )
        context.register_web_api(
            "/astrbot_plugin_sleep_sense/providers",
            self._api_get_providers,
            ["GET"],
            "获取所有已配置的模型提供商列表（供梦境模型下拉选择）",
        )
        context.register_web_api(
            "/astrbot_plugin_sleep_sense/bus_status",
            self._api_get_bus_status,
            ["GET"],
            "获取跨插件联动总线状态（当前总线里有哪些插件、各自状态）",
        )

    async def _api_get_config(self):
        from quart import jsonify
        return jsonify(dict(self.cfg))

    async def _api_post_config(self):
        from quart import request, jsonify
        try:
            body = await request.get_json(force=True, silent=True) or {}
            # 支持两种格式：{ config: {...} } 或直接 flat dict
            data = body.get('config', body)
            for k, v in data.items():
                self.cfg[k] = v
            self.cfg.save_config()
            return jsonify({"ok": True, "success": True})
        except Exception as e:
            logger.error(f"[sleep_sense] 保存配置失败: {e}")
            return jsonify({"ok": False, "msg": str(e)}), 500

    async def _api_get_status(self):
        """返回当前睡眠状态，供 WebUI“当前状态”卡片展示，不依赖是否正在睡眠。"""
        from quart import jsonify
        s = self.state
        cur = s.get("sleep_state", S.AWAKE)
        sleep_start = s.get("sleep_start")
        now = time.time()

        elapsed_min = None
        sleep_start_iso = None
        phase_count = 0
        if cur == S.SLEEPING and sleep_start:
            elapsed_min = int((now - sleep_start) // 60)
            sleep_start_iso = self._local_dt(sleep_start).strftime("%H:%M")
            if self.daily_log and self.daily_log[-1].get("wake_time") is None:
                phase_count = len(self.daily_log[-1].get("phases", []))

        # 预计起床时间：优先用自定义闹钟时间，否则用配置的当天起床时间
        wake_time_str = s.get("custom_wake_time")
        if not wake_time_str:
            wake_time_str = self._get_day_wake_time(self._local_now())

        return jsonify({
            "sleep_state": cur,
            "sleep_cycle_phase": s.get("sleep_cycle_phase", "normal"),
            "sleep_start": sleep_start_iso,
            "elapsed_minutes": elapsed_min,
            "phase_switch_count": phase_count,
            "expected_wake_time": wake_time_str,
            "last_sleep_duration_minutes": (
                int(s.get("last_sleep_duration", 0) // 60) if s.get("last_sleep_duration") else None
            ),
            "is_sleeping": cur == S.SLEEPING,
            "nap_is_critical": s.get("nap_is_critical", False) if cur == S.NAPPING else None,
            "overtime_reason": s.get("overtime_reason") if cur == S.OVERTIME else None,
        })

    async def _api_get_sleep_history(self):
        """汇总最近 N 天（默认 7）的睡眠总时长，直接读 daily_log（按起床当天归类）。"""
        from quart import request, jsonify
        try:
            days = int(request.args.get("days", 7))
        except Exception:
            days = 7
        days = max(1, min(days, 30))

        by_date = {e["date"]: e for e in self.daily_log if e.get("wake_time")}
        today = self._local_now().date()
        result = []
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            key = d.strftime("%Y-%m-%d")
            entry = by_date.get(key)
            minutes = round((entry.get("duration_seconds") or 0) / 60) if entry else 0
            result.append({
                "date": d.strftime("%m/%d"),
                "minutes": minutes,
                "hours": round(minutes / 60, 1),
                "score": entry.get("score") if entry else None,
            })
        return jsonify({"days": days, "history": result})

    async def _api_get_daily_log(self):
        """
        获取某一天的完整睡眠日志：入睡/起床时间、周期阶段轨迹（几点进浅睡/深睡）、
        评分、关联梦境。供 WebUI 画"手表式"睡眠时间线，也供 /睡眠昨天 指令复用。
        date 参数格式 YYYY-MM-DD，不传则：
        - 如果当前正在睡眠中，返回这次还没结束的实时轨迹（in_progress=True）
        - 否则返回最近一条已完成的记录
        """
        from quart import request, jsonify
        date_str = request.args.get("date")
        if date_str:
            entry = next((e for e in self.daily_log if e.get("date") == date_str), None)
            if not entry:
                return jsonify({"found": False})
            return jsonify({"found": True, "log": entry, "in_progress": False})

        # 当前正在睡眠中：daily_log最后一条应该就是这次还没结束的记录
        if self.state.get("sleep_state") == S.SLEEPING and self.daily_log:
            last = self.daily_log[-1]
            if last.get("wake_time") is None:
                # 构造一份"实时快照"：用当前时间当作临时的wake_time，让前端能画出到目前为止的轨迹
                snapshot = dict(last)
                snapshot["wake_time"] = time.time()
                snapshot["wake_time_iso"] = "（睡眠中）"
                snapshot["duration_seconds"] = time.time() - (last.get("sleep_start") or time.time())
                snapshot["score"] = None  # 没结束不打分
                return jsonify({"found": True, "log": snapshot, "in_progress": True})

        finished = [e for e in self.daily_log if e.get("wake_time")]
        entry = finished[-1] if finished else None
        if not entry:
            return jsonify({"found": False})
        return jsonify({"found": True, "log": entry, "in_progress": False})

    async def _api_get_dreams(self):
        """返回梦境记录列表，最多返回最近30条，按时间倒序。"""
        from quart import request, jsonify
        limit = min(int(request.args.get("limit", 20)), 50)
        recent = list(reversed(self.dreams[-limit:]))
        return jsonify({"dreams": recent, "total": len(self.dreams)})

    async def _api_get_alarms(self):
        """返回当前临时闹钟列表。"""
        from quart import jsonify
        return jsonify({"alarms": self.alarms})

    async def _api_get_providers(self):
        """返回所有已配置的模型提供商列表，供WUI做成下拉选择框（梦境生成专用模型）。"""
        from quart import jsonify
        providers = []
        try:
            all_providers = self.context.get_all_providers()
            for p in all_providers:
                try:
                    meta = p.meta()
                    providers.append({"id": meta.id, "model": meta.model, "type": meta.type})
                except Exception:
                    continue
        except Exception as e:
            self._log("warn", f"获取模型列表失败: {e}")
        return jsonify({"providers": providers})

    async def _api_get_bus_status(self):
        """
        读取跨插件联动总线目录下所有插件的状态文件，不只是sleep_sense自己写的那个，
        让WUI能展示"目前总线上有哪些插件接入了、各自处于什么状态"，方便确认联动是否生效，
        以后接入更多插件时也能在这里一并看到，不用每加一个联动就改一次WUI代码。
        """
        from quart import jsonify
        plugins = []
        try:
            bus_dir = _bus_dir()
            now = time.time()
            for f in sorted(bus_dir.glob("*.json")):
                try:
                    with open(f, "r", encoding="utf-8") as fp:
                        data = json.load(fp)
                    expires_at = data.get("expires_at")
                    is_expired = bool(expires_at) and now > expires_at
                    plugins.append({
                        "plugin": data.get("plugin", f.stem),
                        "label": data.get("label", "未知状态"),
                        "do_not_disturb": data.get("do_not_disturb", False) and not is_expired,
                        "updated_at": data.get("updated_at"),
                        "expired": is_expired,
                        "is_self": data.get("plugin") == "sleep_sense",
                    })
                except Exception as e:
                    self._log("debug", f"读取总线文件 {f.name} 失败: {e}")
        except Exception as e:
            self._log("warn", f"读取联动总线失败: {e}")
        return jsonify({"plugins": plugins, "bus_dir": str(_bus_dir())})

    # ═══════════════════════════════════════════════════════════════════
    # 消息主入口
    # ═══════════════════════════════════════════════════════════════════
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        async with self._lock:
            uid      = str(event.get_sender_id())
            is_admin = self._is_admin(uid)
            is_group = _is_group(event)
            is_at    = _is_at_me(event) if is_group else True
            text     = event.message_str or ""

            # 管理员发送的本插件指令（测试/查询/强制切换）永远不被睡眠拦截，也不计入
            # "管理员活跃时间"——否则熙熙拿指令测试插件，会被系统误判成"管理员一直在和他聊天"，
            # 影响入睡/二次入睡等需要"管理员安静下来"才能成立的判断。指令本身不算对话。
            stripped_text = text.lstrip("/").strip()
            own_commands = (
                "睡眠帮助", "睡眠状态", "睡眠周期", "睡眠昨天", "睡眠统计", "睡眠报告",
                "强制清醒", "强制慵懒", "强制睡觉", "强制熬夜", "强制午休",
                "测试做梦", "测试起床", "测试噩梦", "测试闹钟",
                "睡眠日志开", "睡眠日志关",
                "梦境列表", "梦境今晚", "梦境清除", "对方",
            )
            is_own_command = is_admin and stripped_text in own_commands
            if is_own_command:
                return  # 交给 @filter.command 指令处理器去处理，不影响任何睡眠状态判断

            # 屏蔽关键词：优先级最高，命中直接彻底不回复，不进入任何后续逻辑
            if self.cfg.get("block_keywords_enabled", False) and text:
                contains_raw = self.cfg.get("block_keywords_contains", "")
                prefix_raw   = self.cfg.get("block_keywords_prefix", "")
                contains_words = [w.strip() for w in str(contains_raw).split(",") if w.strip()]
                prefix_words   = [w.strip() for w in str(prefix_raw).split(",") if w.strip()]
                if any(w in text for w in contains_words) or any(text.strip().startswith(w) for w in prefix_words):
                    event.should_call_llm(False)
                    return

            if is_admin:
                self._last_admin_ts = time.time()
                self._last_admin_text = text
                if not is_group:
                    self.state["admin_umo"] = event.unified_msg_origin
                    self._save_state()
            else:
                self._last_others_ts[uid] = time.time()

            cur = self.state["sleep_state"]
            if cur in (S.AWAKE, S.LAZY, S.OVERTIME) and text:
                self._collect_dream_ctx(text)

            if cur == S.SLEEPING:
                result = await self._handle_sleeping(event, uid, is_admin, is_group, is_at, text)
                if result is True:
                    pass  # 已吵醒，让LLM正常回复
                elif result is False:
                    event.should_call_llm(False)  # 计数中，阻止LLM响应
                    return
                else:  # None：彻底忽略（群聊无@等）
                    event.should_call_llm(False)
                    return
            elif cur == S.NAPPING:
                result = await self._handle_napping(event, uid, is_admin, is_group, is_at)
                if result is True:
                    pass
                elif result is False:
                    event.should_call_llm(False)
                    return
                else:
                    event.should_call_llm(False)
                    return
            else:
                inject = self._build_inject(is_admin)
                if inject:
                    self._inject_prompt(event, inject)

    # ═══════════════════════════════════════════════════════════════════
    # 睡觉中处理
    # ═══════════════════════════════════════════════════════════════════
    async def _handle_sleeping(self, event, uid, is_admin, is_group, is_at, text):
        """
        返回值：
        True  = 已触发吵醒，可以让LLM回复
        False = 正在计数中（还没到吵醒阈值），需要拦截LLM，不让回复
        None  = 彻底忽略（群聊没@、场景关闭等），不做任何处理也不拦截
        """
        if not self.cfg.get("wake_trigger_enabled", True):
            return None
        if is_group and not is_at:
            return None  # 群聊没@，彻底忽略，不拦截
        if not is_admin and not is_group and not self.cfg.get("scene_private_others", True):
            return None
        if not is_admin and is_group and not self.cfg.get("scene_group_others", True):
            return None
        if is_admin and not is_group and not self.cfg.get("scene_private_admin", True):
            return None
        if is_admin and is_group and not self.cfg.get("scene_group_admin", True):
            return None

        if is_admin:
            alert_raw = self.cfg.get("wake_alert_words", "醒醒,有大事,紧急,快起来")
            alerts = [w.strip() for w in str(alert_raw).split(",") if w.strip()]
            if not is_group or self.cfg.get("wake_alert_group_enabled", True):
                if any(w in text for w in alerts):
                    await self._do_wake(event, uid, is_admin, is_group, force=True, trigger_count=1)
                    return True

        multiplier = self._get_cycle_multiplier()
        scope = self._get_msg_scope_id(event, uid, is_group)
        key = f"wake_{scope}"
        cnt = self._msg_counters.get(key, 0) + 1
        self._msg_counters[key] = cnt

        if is_admin:
            needed = int(self.cfg.get("wake_count_private_admin", 3)) if not is_group else int(self.cfg.get("wake_count_group_admin", 3))
        else:
            needed = int(self.cfg.get("wake_count_private_others", 10)) if not is_group else int(self.cfg.get("wake_count_group_others", 6))

        if cnt >= max(1, round(needed * multiplier)):
            self._msg_counters[key] = 0
            await self._do_wake(event, uid, is_admin, is_group, trigger_count=cnt,
                                needed_base=needed, multiplier=multiplier)
            return True
        # 没达到吵醒阈值，但记录一下"谁尝试联系过他"，供醒来后判断要不要主动回应
        self._note_attempted_contact(uid, is_admin, is_group, event)
        return False  # 计数中，需要拦截LLM

    def _note_attempted_contact(self, uid: str, is_admin: bool, is_group: bool, event):
        """睡眠中有人发消息但没达到吵醒阈值，记一下，供起床后判断是否要主动回应这些人。"""
        if not self.daily_log:
            return
        entry = self.daily_log[-1]
        if entry.get("wake_time") is not None:
            return
        contacts = entry.get("attempted_contacts", [])
        # 同一个人在同一个场景的多次尝试，更新次数而不是无限堆条目
        try:
            gid = event.get_group_id() if is_group else None
        except Exception:
            gid = None
        existing = next((c for c in contacts if c.get("uid") == uid and c.get("group_id") == gid), None)
        if existing:
            existing["count"] += 1
            existing["last_ts"] = time.time()
        else:
            contacts.append({
                "uid": uid, "is_admin": is_admin, "is_group": is_group,
                "group_id": gid, "count": 1, "last_ts": time.time(),
                "first_time": self._local_dt(time.time()).strftime("%H:%M"),
            })
        entry["attempted_contacts"] = contacts
        self._save_json(DAILY_LOG_PATH, self.daily_log)

    async def _do_wake(self, event, uid, is_admin, is_group, force=False, trigger_count: int = 0,
                        needed_base: int = 0, multiplier: float = 1.0):
        woken_key = f"woken_{uid}_{is_group}"
        wake_msg_count = self._woken_msg_count.get(woken_key, 0) + 1
        self._woken_msg_count[woken_key] = wake_msg_count
        prefix = self._get_cycle_prompt_prefix()

        if wake_msg_count == 1:
            if is_admin and not is_group:
                p = self.cfg.get("prompts_wake_private_admin_1", "这是你的管理员，你刚才被吵醒了，很懵很困，可以单发一个「？」或者「嗯？怎么了」。")
            elif is_admin and is_group:
                p = self.cfg.get("prompts_wake_group_admin_1", "管理员在群里艾特你了，把你吵醒了，很懵很困，可以短回复一下。")
            elif not is_group:
                p = self.cfg.get("prompts_wake_private_others_1", "你被他吵醒了，很懵，可以发个问号或者「嗯？」之类的短回复。")
            else:
                p = self.cfg.get("prompts_wake_group_others_1", "你被群里的人艾特吵醒了，很懵，可以简短问一下怎么了。")
            self._note_woken(uid=uid, is_admin=is_admin, is_group=is_group, trigger_count=trigger_count,
                              needed_base=needed_base, multiplier=multiplier)
            if not is_admin:
                self._note_woken_by_others(is_group)
            self._set_state(S.AWAKE)
            self.state["resleep_pending"] = True  # 标记"刚被吵醒"，进入二次入睡待命窗口
            self.state["goodnight_said_today"] = False  # 这轮睡眠结束了，二次入睡前可以再说一次晚安
            # 同理：更新last_wake_ts，避免等待二次入睡期间被_check_sleep绕过保护
            self.state["last_wake_ts"] = time.time()
            # 给一段冷却期，避免管理员断断续续聊天时，二次入睡又立刻被打断，反复秒睡秒醒
            cooldown_min = int(self.cfg.get("resleep_wake_cooldown_minutes", 5))
            self.state["resleep_cooldown_until"] = time.time() + cooldown_min * 60
            self._save_state()
            self._emit_state("awake")
            # 注意：这里不收尾daily_log记录！被吵醒只是临时打断，不是真正起床。
            # 如果立刻close掉，二次入睡时就只能新开一条记录，导致整晚被拆成两段，
            # 只能看到最后一段的时长和轨迹。真正的收尾交给_do_wake_up（自然醒）
            # 或者确认不会再二次入睡的时候（见_check_resleep里接近起床时间的判断）。
            self._log("info", f"被唤醒 uid={uid} group={is_group}")
        elif wake_msg_count == 2:
            # 同一次唤醒事件里的第二条消息：语气从懵转向清醒。管理员这一档必经，不受多次吵醒开关限制
            if is_admin and not is_group:
                p = self.cfg.get("prompts_wake_private_admin_2", "你刚被吵醒，现在要清醒一点了，语气还是困。看对方回复，如果不是大事，回复简短一点。")
            elif not is_group:
                p = self.cfg.get("prompts_wake_private_others_2", "你刚刚被吵醒，慢慢清醒，语气还是困，如果不是大事回复简短。")
            else:
                p = ""  # 群聊场景暂沿用原有"多次吵醒"逻辑，不单独拆第二波
        else:
            if not self.cfg.get("multi_wake_enabled", True):
                return
            if not is_admin and not self.cfg.get("multi_wake_others", False):
                return
            if is_admin and not is_group:
                p = self.cfg.get("prompts_multi_wake_private_admin", "这是管理员再次发消息吵醒你了，你不生气，只是有点疑惑比较困，回复简短。")
            elif is_admin and is_group:
                p = "这是管理员在群里再次吵醒你，你不生气，有点困，回复简短，可以问问为什么还没睡。"
            else:
                p = self.cfg.get("prompts_wake_private_others_2", "你刚刚被吵醒，慢慢清醒，语气还是困，如果不是大事回复简短。")

        if p:
            self._inject_prompt(event, (prefix + p).strip())
        if wake_msg_count == 1 and not is_admin and self.cfg.get("wake_report_to_admin", True):
            asyncio.create_task(self._report_to_admin_later())

    # ═══════════════════════════════════════════════════════════════════
    # 午休/补觉处理
    # ═══════════════════════════════════════════════════════════════════
    async def _handle_napping(self, event, uid, is_admin, is_group, is_at):
        if is_group and not is_at:
            return None  # 群聊没@，彻底忽略
        scope = self._get_msg_scope_id(event, uid, is_group)
        key = f"nap_{scope}"
        cnt = self._msg_counters.get(key, 0) + 1
        self._msg_counters[key] = cnt
        needed = (
            int(self.cfg.get("nap_wake_private_admin", 3)) if (is_admin and not is_group) else
            int(self.cfg.get("nap_wake_group_admin", 2))   if (is_admin and is_group)     else
            int(self.cfg.get("nap_wake_private_others", 4)) if not is_group               else
            int(self.cfg.get("nap_wake_group_others", 3))
        )
        if cnt >= needed:
            self._msg_counters[key] = 0
            nap_start = self.state.get("nap_start")
            if nap_start:
                nap_duration = time.time() - nap_start
                self.state["last_sleep_duration"] = self.state.get("last_sleep_duration", 0) + nap_duration
            self.state["nap_start"] = None
            self.state["nap_is_critical"] = False
            # 同理：被吵醒退出午休也要更新last_wake_ts，跟_finish_nap一样的原因
            self.state["last_wake_ts"] = time.time()
            # 被吵醒后给一段冷却期，避免管理员还在持续聊天时，系统又立刻判定"严重补觉"
            # 重新把他塞回午休状态，导致不断"秒睡秒醒"的死循环
            cooldown_min = int(self.cfg.get("nap_wake_cooldown_minutes", 10))
            self.state["nap_cooldown_until"] = time.time() + cooldown_min * 60
            self._set_state(S.AWAKE)
            self._emit_state("awake")
            if is_admin and not is_group:
                p = "刚才是在午休，被管理员吵醒了，回复一下吧。"
            elif is_admin and is_group:
                p = "刚才在午休，管理员在群里艾特了，睡着了，回复一下。"
            elif not is_group:
                p = "你刚才在午休，被对方发消息吵醒了。"
            else:
                p = "你刚才在补觉，被群聊里有人艾特吵醒了。如果问你去哪了，可以回复刚才睡着了。"
            self._inject_prompt(event, p)
            return True
        return False  # 计数中，拦截LLM

    # ═══════════════════════════════════════════════════════════════════
    # 清醒状态提示词注入
    # ═══════════════════════════════════════════════════════════════════
    def _build_inject(self, is_admin: bool) -> str:
        parts = []
        cur = self.state["sleep_state"]
        if cur == S.LAZY:
            p = self.cfg.get("lazy_prompt", "")
            if p: parts.append(p)
        if cur == S.AWAKE:
            consec = self.state.get("consecutive_overtime", 0)
            fs = int(self.cfg.get("overtime_fatigue_start_day", 2))
            if consec >= fs:
                day = min(consec, 4)
                key = {1:"prompts_fatigue_day1",2:"prompts_fatigue_day2",3:"prompts_fatigue_day3",4:"prompts_fatigue_day4"}.get(day,"prompts_fatigue_day4")
                fp = self.cfg.get(key, "")
                if fp: parts.append(fp)
            if not is_admin and self.state.get("resleep_grace_started"):
                rp = self.cfg.get("prompts_resleep_others_grace", "")
                if rp: parts.append(rp)
            if not is_admin and self.state.get("presleep_pending"):
                sp = self.cfg.get("sleep_others_chatting_prompt", "")
                if sp: parts.append(sp)
        if cur == S.OVERTIME:
            scope = self.cfg.get("overtime_night_scope", "admin_only")
            if scope == "global" or is_admin:
                p = self.cfg.get("overtime_night_mood_prompt", "")
                if p: parts.append(p)
                if is_admin:
                    parts.append("如果聊着聊着觉得困了、或者想结束熬夜了，可以调用 sleep_end_overtime 工具去睡觉。")
        return " ".join(p for p in parts if p)

    # ═══════════════════════════════════════════════════════════════════
    # 指令：/睡眠
    # ═══════════════════════════════════════════════════════════════════
    async def _check_admin(self, uid: str) -> bool:
        if self._is_admin(uid):
            return True
        try:
            return await self.context.is_admin(uid)
        except Exception:
            return False

    @filter.command("睡眠帮助")
    async def cmd_sleep_help(self, event: AstrMessageEvent):
        """查看所有指令。用法: /睡眠帮助"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        yield event.plain_result(
            "🌙 sleep_sense 指令帮助\n"
            "━━━━━━━━━━━━━━━\n"
            "📊 查询\n"
            "/睡眠状态    — 当前状态\n"
            "/睡眠周期    — 当前睡眠周期详情\n"
            "/睡眠昨天    — 昨晚完整时间线+评分\n"
            "/睡眠统计    — 本周睡眠统计\n"
            "/睡眠报告    — 生成晚安报告图片\n"
            "/对方        — 查看陆渊实时状态\n"
            "\n🎮 强制切换\n"
            "/强制清醒  /强制慵懒\n"
            "/强制睡觉  /强制熬夜  /强制午休\n"
            "\n🧪 测试\n"
            "/测试做梦  — 立刻生成梦境\n"
            "/测试起床  — 触发起床流程\n"
            "/测试噩梦  — 触发噩梦提示词\n"
            "/测试闹钟  — 查看当前闹钟\n"
            "\n⚙️ 其他\n"
            "/睡眠日志开 / 睡眠日志关\n"
            "\n💤 梦境\n"
            "/梦境列表 / 梦境今晚 / 梦境清除"
        )

    @filter.command("睡眠状态")
    async def cmd_sleep_status(self, event: AstrMessageEvent):
        """查看当前睡眠状态。用法: /睡眠状态"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        s = self.state
        dur = s.get("last_sleep_duration", 0)
        cur_state = s.get("sleep_state", "?")
        if cur_state == S.NAPPING:
            state_label = "☕ 严重补觉中" if s.get("nap_is_critical") else "☕ 午休中"
        else:
            state_cn = {S.AWAKE:"😊 清醒",S.LAZY:"😴 慵懒",S.SLEEPING:"💤 睡眠中",S.OVERTIME:"🌃 熬夜"}
            state_label = state_cn.get(cur_state, "?")
        lines = [
            f"🌙 状态：{state_label}",
            f"⏰ 上次睡眠：{dur/3600:.1f}h",
            f"🌃 连续熬夜：{s.get('consecutive_overtime',0)}天",
            f"📅 本周熬夜：{s.get('weekly_overtime',0)}天",
        ]
        if cur_state == S.OVERTIME and s.get("overtime_reason"):
            lines.append(f"💭 熬夜原因：{s.get('overtime_reason')}")
        yield event.plain_result("\n".join(lines))

    @filter.command("睡眠周期")
    async def cmd_sleep_cycle(self, event: AstrMessageEvent):
        """查看当前睡眠周期详情。用法: /睡眠周期"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        s = self.state
        if s.get("sleep_state") != S.SLEEPING:
            yield event.plain_result("当前不在睡眠中，无睡眠周期数据。")
            return
        start   = s.get("sleep_start") or time.time()
        elapsed = (time.time() - start) / 60
        phase   = s.get("sleep_cycle_phase", "normal")
        phase_cn = {"light":"🌙 浅睡期（容易被吵醒）","deep":"😴 深睡期（很难被吵醒）","normal":"💤 正常睡眠期"}
        switches = 0
        if self.daily_log and self.daily_log[-1].get("wake_time") is None:
            switches = len(self.daily_log[-1].get("phases", [])) - 1
        yield event.plain_result(
            f"💤 当前睡眠周期\n"
            f"━━━━━━━━━━━━━\n"
            f"已睡：{elapsed:.0f} 分钟\n"
            f"当前：{phase_cn.get(phase, phase)}\n"
            f"今晚已切换：{switches} 次（浅睡↔深睡反复波动中）\n"
            f"吵醒倍数：×{self._get_cycle_multiplier()}"
        )

    @filter.command("睡眠昨天")
    async def cmd_sleep_yesterday(self, event: AstrMessageEvent):
        """查看昨晚完整时间线和评分。用法: /睡眠昨天"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        finished = [e for e in self.daily_log if e.get("wake_time")]
        if not finished:
            yield event.plain_result("还没有完整的睡眠记录。")
            return
        entry = finished[-1]
        phase_cn = {"light": "浅睡", "deep": "深睡", "normal": "正常"}
        lines = [f"😴 {entry.get('date','')} 睡眠时间线", "━━━━━━━━━━━━━"]
        if entry.get("overtime_reason"):
            lines.append(f"🌃 睡前熬夜原因：{entry.get('overtime_reason')}")
        lines.append(f"入睡：{entry.get('sleep_start_iso','?')}　起床：{entry.get('wake_time_iso','?')}")
        dur_h = (entry.get("duration_seconds") or 0) / 3600
        lines.append(f"总时长：{dur_h:.1f} 小时　被吵醒：{entry.get('woken_count',0)} 次")
        woken_reasons = entry.get("woken_reasons", [])
        if woken_reasons:
            for r in woken_reasons:
                cnt_str = f"（第{r.get('trigger_count')}条消息触发）" if r.get('trigger_count') else ""
                needed_base, mult = r.get('needed_base'), r.get('multiplier')
                if needed_base and mult and mult != 1:
                    actual_needed = max(1, round(needed_base * mult))
                    cnt_str += f"（原需{needed_base}条×{mult}倍={actual_needed}条）"
                lines.append(f"  - {r.get('time','?')} 在{r.get('phase_cn','?')}被{r.get('who','对方')}{r.get('scene','')}吵醒{cnt_str}")
        lines.append("")
        lines.append("🕐 阶段轨迹：")
        for p in entry.get("phases", []):
            lines.append(f"  {p.get('time','?')} 进入 {phase_cn.get(p.get('phase'), p.get('phase'))}")
        score = entry.get("score")
        if score:
            lines.append("")
            lines.append(f"📊 睡眠评分：{score.get('total','?')} 分（{score.get('grade','')}）")
            lines.append(f"  时长 {score.get('duration_score','?')}/40　深睡占比 {score.get('deep_score','?')}/25（{score.get('deep_ratio_pct','?')}%）")
            lines.append(f"  连续性 {score.get('continuity_score','?')}/20　及时性 {score.get('timeliness_score','?')}/15")
        dream = entry.get("dream")
        if dream and dream.get("clarity") != "forgotten":
            lines.append("")
            lines.append(f"💭 做了个梦（{dream.get('type','')}），{'记得清楚' if dream.get('clarity')=='clear' else '有点模糊'}")
        yield event.plain_result("\n".join(lines))

    @filter.command("睡眠统计")
    async def cmd_sleep_stats(self, event: AstrMessageEvent):
        """查看本周睡眠统计。用法: /睡眠统计"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        s = self.state
        week    = datetime.now().strftime("%Y-W%W")
        data    = self._load_json(STATS_DIR / f"{week}.json", {"records": []})
        records = data.get("records", [])
        yield event.plain_result(
            f"📊 本周睡眠统计（{week}）\n"
            f"━━━━━━━━━━━━━\n"
            f"入睡次数：{sum(1 for r in records if r.get('key')=='sleep_start')}次\n"
            f"起床次数：{sum(1 for r in records if r.get('key')=='wake_time')}次\n"
            f"梦境记录：{len(self.dreams)}条\n"
            f"上次睡眠：{s.get('last_sleep_duration',0)/3600:.1f}h\n"
            f"连续熬夜：{s.get('consecutive_overtime',0)}天"
        )

    @filter.command("睡眠报告")
    async def cmd_sleep_report(self, event: AstrMessageEvent):
        """生成晚安报告图片。用法: /睡眠报告"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        if not self.cfg.get("sleep_report_enabled", True):
            yield event.plain_result("睡眠报告功能未开启。")
            return
        finished = [e for e in self.daily_log if e.get("wake_time")]
        if not finished:
            yield event.plain_result("还没有完整的睡眠记录，报告无法生成。")
            return
        yield event.plain_result("生成中，稍等一下…")
        log = finished[-1]
        overlap_h = self._calc_overlap_hours()
        overlap_hint = ""
        if overlap_h is not None and overlap_h > 0:
            overlap_hint = f"你们今天醒着的重叠时间大约 {overlap_h} 小时"
        elif overlap_h == 0:
            overlap_hint = "今天时差太大，醒着的时间几乎没有重叠"
        img_path = await asyncio.get_event_loop().run_in_executor(
            None, self._render_sleep_report, log, overlap_hint
        )
        if img_path:
            import astrbot.api.message_components as Comp
            yield event.chain_result([Comp.Image.fromFileSystem(img_path)])
        else:
            yield event.plain_result("图片渲染失败，请确认服务器已安装 Pillow。")

    @filter.command("强制清醒")
    async def cmd_force_awake(self, event: AstrMessageEvent):
        """强制切换为清醒状态。用法: /强制清醒"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        self._set_state(S.AWAKE); self._emit_state("awake")
        yield event.plain_result("✅ 已强制切换：清醒")

    @filter.command("强制慵懒")
    async def cmd_force_lazy(self, event: AstrMessageEvent):
        """强制切换为慵懒状态。用法: /强制慵懒"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        self._set_state(S.LAZY)
        yield event.plain_result("✅ 已强制切换：慵懒")

    @filter.command("强制睡觉")
    async def cmd_force_sleep(self, event: AstrMessageEvent):
        """强制进入睡眠。用法: /强制睡觉"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        await self._enter_sleep()
        yield event.plain_result("✅ 已强制切换：睡眠")

    @filter.command("强制熬夜")
    async def cmd_force_overtime(self, event: AstrMessageEvent):
        """强制切换为熬夜状态。用法: /强制熬夜"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        self._set_state(S.OVERTIME)
        self.state["consecutive_overtime"] = self.state.get("consecutive_overtime", 0) + 1
        self.state["overtime_since"] = time.time()
        self._save_state()
        yield event.plain_result("✅ 已强制切换：熬夜")

    @filter.command("强制午休")
    async def cmd_force_nap(self, event: AstrMessageEvent):
        """强制进入午休/补觉。用法: /强制午休"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        await self._enter_nap()
        yield event.plain_result("✅ 已强制切换：午休")

    @filter.command("测试做梦")
    async def cmd_test_dream(self, event: AstrMessageEvent):
        """立刻生成一次梦境（测试用）。用法: /测试做梦"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        if self.state["sleep_state"] != S.SLEEPING:
            self.state["sleep_start"] = time.time() - 6000
            self.state["sleep_cycle_phase"] = "deep"
        self._dream_generated_tonight = False
        yield event.plain_result("🌌 正在生成梦境，请稍候（约10~30秒）…")
        await self._generate_dream()
        dream = self.state.get("tonight_dream")
        if dream:
            yield event.plain_result(
                f"✅ 梦境已生成\n"
                f"类型：{dream.get('type','')}  清晰度：{dream.get('clarity','')}\n\n"
                f"{dream.get('content','')}"
            )
        else:
            yield event.plain_result("⚠️ 梦境生成失败，请检查 LLM 配置或日志。")

    @filter.command("测试起床")
    async def cmd_test_wake(self, event: AstrMessageEvent):
        """触发起床流程（测试用）。用法: /测试起床"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        await self._do_wake_up()
        yield event.plain_result("✅ 已触发起床流程（含梦境浮现）")

    @filter.command("测试噩梦")
    async def cmd_test_nightmare(self, event: AstrMessageEvent):
        """触发噩梦惊醒提示词（测试用）。用法: /测试噩梦"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        self.state["nightmare_tonight"] = False
        p = self.cfg.get("nightmare_prompt", "你刚才做噩梦惊醒了，现在心里有点难受，可以发消息给管理员寻求安慰。")
        msg = await self._generate_proactive_message(p)
        if msg:
            await self._send_to_admin(msg)
        self._set_state(S.AWAKE); self._emit_state("awake")
        yield event.plain_result("✅ 已触发噩梦惊醒提示词")

    @filter.command("测试闹钟")
    async def cmd_test_alarm(self, event: AstrMessageEvent):
        """查看当前临时闹钟列表。用法: /测试闹钟"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        if not self.alarms:
            yield event.plain_result("当前没有临时闹钟。")
        else:
            lines = [f"⏰ {a.get('date','')} {a.get('time','')} — {a.get('reason','')}" for a in self.alarms]
            yield event.plain_result("当前闹钟：\n" + "\n".join(lines))

    @filter.command("睡眠日志开")
    async def cmd_log_on(self, event: AstrMessageEvent):
        """开启debug日志。用法: /睡眠日志开"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        self._log_level = "debug"
        yield event.plain_result("✅ debug 日志已开启")

    @filter.command("睡眠日志关")
    async def cmd_log_off(self, event: AstrMessageEvent):
        """关闭debug日志。用法: /睡眠日志关"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足，仅管理员可用"); return
        self._log_level = "info"
        yield event.plain_result("✅ debug 日志已关闭")

    # ═══════════════════════════════════════════════════════════════════
    # 指令：梦境（扁平独立指令）
    # ═══════════════════════════════════════════════════════════════════
    @filter.command("梦境列表")
    async def cmd_dream_list(self, event: AstrMessageEvent):
        """查看最近的梦境记录列表。用法: /梦境列表"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足"); return
        clarity_cn = {"clear":"记得清楚","blurry":"有点模糊","feeling_only":"只剩感觉","forgotten":"完全忘了"}
        if not self.dreams:
            yield event.plain_result("📖 暂无梦境记录")
            return
        lines = [f"📅 {d.get('date','')} [{d.get('type','')}] {clarity_cn.get(d.get('clarity',''),'?')}" for d in self.dreams[-5:]]
        yield event.plain_result("最近5条梦境记录：\n" + "\n".join(lines))

    @filter.command("梦境今晚")
    async def cmd_dream_tonight(self, event: AstrMessageEvent):
        """查看今晚的梦境详情。用法: /梦境今晚"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足"); return
        clarity_cn = {"clear":"记得清楚","blurry":"有点模糊","feeling_only":"只剩感觉","forgotten":"完全忘了"}
        dream = self.state.get("tonight_dream")
        if not dream and self.dreams:
            d = self.dreams[-1]
            if d.get("clarity") != "forgotten":
                yield event.plain_result(
                    f"最近一次（{d.get('date','')}）\n"
                    f"类型：{d.get('type','')}  清晰度：{clarity_cn.get(d.get('clarity',''),'?')}\n\n"
                    f"{d.get('content','')}"
                )
                return
        if dream:
            yield event.plain_result(
                f"今晚的梦 [{dream.get('type','')}]\n"
                f"清晰度：{clarity_cn.get(dream.get('clarity',''),'?')}\n\n"
                f"{dream.get('content','')}"
            )
        else:
            yield event.plain_result("今晚还没有梦境记录，或已经忘记了。")

    @filter.command("梦境清除")
    async def cmd_dream_clear(self, event: AstrMessageEvent):
        """清除全部梦境记录。用法: /梦境清除"""
        if not await self._check_admin(str(event.get_sender_id())):
            yield event.plain_result("❌ 权限不足"); return
        self.dreams = []
        self._save_json(DREAMS_PATH, self.dreams)
        self.state.pop("tonight_dream", None)
        self._save_state()
        yield event.plain_result("✅ 梦境记录已清除")

    # ═══════════════════════════════════════════════════════════════════
    # AI 工具：设置临时闹钟
    # ═══════════════════════════════════════════════════════════════════
    @filter.command("对方")
    async def cmd_partner(self, event: AstrMessageEvent):
        """查看陆渊当前实时睡眠状态（异地恋专用）。/对方"""
        uid = str(event.get_sender_id())
        if not self._is_admin(uid):
            yield event.plain_result("❌ 仅管理员可以查询")
            return

        s = self.state
        cur = s.get("sleep_state", S.AWAKE)
        state_cn = {
            S.AWAKE: "清醒", S.LAZY: "有点困了", S.SLEEPING: "睡着了",
            S.NAPPING: "在补觉", S.OVERTIME: "在熬夜"
        }
        state_emoji = {
            S.AWAKE: "😊", S.LAZY: "😪", S.SLEEPING: "💤",
            S.NAPPING: "☕", S.OVERTIME: "🌃"
        }
        lines = [f"{state_emoji.get(cur,'')} 陆渊现在{state_cn.get(cur,'?')}"]

        sleep_start = s.get("sleep_start")
        if cur == S.SLEEPING and sleep_start:
            elapsed_min = int((time.time() - sleep_start) // 60)
            phase = s.get("sleep_cycle_phase", "normal")
            phase_cn = {"light": "浅睡期", "deep": "深睡期", "normal": "正常睡眠"}
            lines.append(f"已睡 {elapsed_min} 分钟，当前处于{phase_cn.get(phase, phase)}")
            wake_str = s.get("custom_wake_time") or self._get_day_wake_time(self._local_now())
            lines.append(f"预计 {wake_str} 起床")

        # 最近一晚记录
        finished = [e for e in self.daily_log if e.get("score")]
        if finished:
            last = finished[-1]
            sc = last.get("score", {})
            lines.append(f"上次睡眠评分：{sc.get('total','?')} 分（{sc.get('grade','')}）")

        # 重叠时间
        overlap_h = self._calc_overlap_hours()
        if overlap_h is not None:
            if overlap_h > 0:
                lines.append(f"今天你们醒着重叠大约 {overlap_h} 小时")
            else:
                lines.append("今天时差太大，几乎没有重叠时间")

        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="sleep_set_alarm")
    async def tool_set_alarm(self, event: AstrMessageEvent, time_str: str, reason: str, target: str = ""):
        """设置临时闹钟，让Bot在指定时间自动唤醒。

        Args:
            time_str (str): 时间，格式 HH:MM，如 06:30
            reason (str): 闹钟原因说明
            target (str): 目标用户QQ号，可留空
        """
        if not self.cfg.get("alarm_enabled", True):
            yield event.plain_result("临时闹钟功能已关闭。")
            return
        max_cnt = int(self.cfg.get("alarm_max_count", 3))
        if len(self.alarms) >= max_cnt:
            yield event.plain_result(f"闹钟已满（最多 {max_cnt} 个）。")
            return
        now      = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        try:
            alarm_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            if alarm_dt <= now:
                alarm_dt += timedelta(days=1)
                date_str  = alarm_dt.strftime("%Y-%m-%d")
        except ValueError:
            yield event.plain_result("时间格式错误，请用 HH:MM，如 06:30")
            return
        self.alarms.append({"time": time_str, "date": date_str, "reason": reason, "target": target, "created_at": int(time.time())})
        self._save_json(ALARMS_PATH, self.alarms)
        yield event.plain_result(f"✅ 闹钟已设置：{date_str} {time_str}，原因：{reason}")

    @filter.llm_tool(name="sleep_decide_overtime")
    async def tool_decide_overtime(self, event: AstrMessageEvent, decision: str, reason: str = ""):
        """被询问"今天晚上要不要熬夜"后，调用这个工具正式做出决定。
        必须调用这个工具，单纯用文字回答"是"或"否"不会真正生效。

        Args:
            decision (str): "是" 表示决定熬夜，"否" 表示决定正常睡觉
            reason (str): 简短说明为什么这么决定（比如"今天被吵醒很多次，不想睡"），会被记录下来
        """
        is_overtime = decision.strip() in ("是", "熬夜", "yes", "true", "1")
        if is_overtime:
            self._set_state(S.OVERTIME)
            self.state["consecutive_overtime"] = self.state.get("consecutive_overtime", 0) + 1
            self.state["weekly_overtime"] = self.state.get("weekly_overtime", 0) + 1
            self.state["overtime_reason"] = reason or "自行决定熬夜"
            self.state["overtime_since"] = time.time()
            self._save_state()
            self._log("info", f"AI决定熬夜，原因：{reason}")
            yield event.plain_result(f"好，今晚熬夜了。{reason}" if reason else "好，今晚熬夜了。")
        else:
            self.state["overtime_reason"] = None
            self._save_state()
            self._log("info", f"AI决定不熬夜，正常睡觉。原因：{reason}")
            yield event.plain_result("好，今晚正常睡觉。")

    @filter.llm_tool(name="sleep_end_overtime")
    async def tool_end_overtime(self, event: AstrMessageEvent, reason: str = ""):
        """熬夜过程中，结合当前聊天上下文，自己决定不再熬夜、要去睡觉了时调用这个工具。
        熬夜状态不会自动结束，必须自己主动决定才会真正去睡，否则会一直熬夜下去。

        Args:
            reason (str): 简短说明为什么这时候决定去睡了（比如"困了""陪她陪到这就够了"）
        """
        if self.state.get("sleep_state") != S.OVERTIME:
            return
        self._log("info", f"AI决定结束熬夜，去睡觉。原因：{reason}")
        await self._enter_sleep()
        yield event.plain_result(f"好，不熬了，去睡觉。{reason}" if reason else "好，不熬了，去睡觉。")




    @filter.llm_tool(name="sleep_set_wake_time")
    async def tool_set_wake_time(self, event: AstrMessageEvent, time_str: str):
        """周末/节假日或熬夜后，自主决定今晚要晚点起床时，调用这个工具设置今晚的起床时间。
        不能比用户平时配置的起床时间晚太多，插件会自动校验范围。

        Args:
            time_str (str): 决定晚点起床的时间，格式 HH:MM，如 09:30
        """
        if not self.cfg.get("weekend_auto_adjust_enabled", True):
            yield event.plain_result("周末作息自主调整功能已关闭。")
            return
        try:
            chosen = self._parse_time(time_str)
        except Exception:
            yield event.plain_result("时间格式错误，请用 HH:MM，如 09:30")
            return
        normal = self._parse_time(self._get_day_wake_time(self._local_now()))
        # 最多比平时晚 3 小时，避免 AI 自己决定睡到下午
        max_minutes = normal.hour * 60 + normal.minute + 180
        chosen_minutes = chosen.hour * 60 + chosen.minute
        if chosen_minutes > max_minutes:
            chosen_minutes = max_minutes
            chosen = self._parse_time(f"{chosen_minutes // 60:02d}:{chosen_minutes % 60:02d}")
        # 保护：不能比平时起床时间早太多（比如AI误传了一个已经过去的时间点），
        # 否则下次入睡后第一次检查就会立刻被唤醒，跟"特殊起床全天误触发"是同一类问题
        min_minutes = normal.hour * 60 + normal.minute - 120
        if chosen_minutes < max(0, min_minutes):
            yield event.plain_result(f"这个时间比平时起床时间还早不少，不像是\"晚点起床\"，已忽略这次设置。")
            return
        final_str = f"{chosen.hour:02d}:{chosen.minute:02d}"
        self.state["custom_wake_time"] = final_str
        self._save_state()
        yield event.plain_result(f"✅ 已记下，今晚晚点起床，预计 {final_str} 起床。")

    # ═══════════════════════════════════════════════════════════════════
    # 后台调度
    # ═══════════════════════════════════════════════════════════════════
    async def _scheduler_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"[sleep_sense] scheduler error: {e}")

    async def _tick(self):
        now = self._local_now()
        cur = self.state["sleep_state"]

        # 慵懒
        if cur == S.AWAKE and self.cfg.get("lazy_enabled", True):
            ls = self._parse_time(self.cfg.get("lazy_start", "22:00"))
            le = self._parse_time(self.cfg.get("lazy_end",   "23:00"))
            if self._in_range(now.time(), ls, le):
                self._set_state(S.LAZY)

        cur = self.state["sleep_state"]  # 重读，防止慵懒切换后状态不同步
        if cur in (S.AWAKE, S.LAZY):
            await self._check_sleep(now)

        cur = self.state["sleep_state"]  # 重读，_check_sleep可能已触发入睡
        if cur == S.SLEEPING:
            await self._check_wake(now)
            self._update_cycle()
            await self._check_nightmare()

        cur = self.state["sleep_state"]  # 重读
        if cur == S.AWAKE:
            await self._check_nap(now)
            await self._check_resleep(now)

        cur = self.state["sleep_state"]  # 重读，午休/补觉可能需要自然结束
        if cur == S.NAPPING:
            await self._check_nap_wake(now)

        cur = self.state["sleep_state"]  # 重读，确保没有在睡眠中触发熬夜询问
        if cur in (S.AWAKE, S.LAZY):
            await self._check_overtime_decision(now)
            await self._check_weekend_adjust(now)

        cur = self.state["sleep_state"]
        if cur == S.OVERTIME:
            await self._check_overtime_safety(now)

    async def _check_overtime_safety(self, now: datetime):
        """
        熬夜结束有两层保护：
        ①自然退出：管理员安静了一段时间没理他，他会觉得"算了不熬了"，自己去睡
          （跟正常清醒状态下"管理员空闲多久该睡觉"是同一个思路，只是用在熬夜场景）
        ②硬性上限：万一管理员一直在、AI也一直没自己决定睡，熬夜超过最大时长后兜底强制睡觉，
          避免无限期卡在熬夜状态
        """
        overtime_since = self.state.get("overtime_since")
        if not overtime_since:
            self.state["overtime_since"] = time.time()
            self._save_state()
            return

        # ①自然退出：管理员空闲够久，且熬夜也持续了一定时间（避免刚决定熬夜几分钟就被判定"没人理"）
        if self.cfg.get("overtime_auto_end_enabled", True):
            min_minutes = int(self.cfg.get("overtime_min_minutes", 20))
            idle_minutes = int(self.cfg.get("overtime_admin_idle_minutes", 30))
            elapsed_min = (time.time() - overtime_since) / 60
            admin_idle_min = (time.time() - self._last_admin_ts) / 60
            if elapsed_min >= min_minutes and admin_idle_min >= idle_minutes:
                self._log("info", f"熬夜中管理员已空闲{admin_idle_min:.0f}分钟，自己决定不熬了去睡")
                await self._enter_sleep()
                return

        # ②硬性上限：兜底保护
        max_hours = float(self.cfg.get("overtime_max_hours", 6))
        if time.time() - overtime_since >= max_hours * 3600:
            self._log("info", f"熬夜超过{max_hours}小时仍未主动结束，强制进入睡眠")
            await self._enter_sleep()

    async def _check_sleep(self, now: datetime):
        if not self.cfg.get("sleep_enabled", True):
            return
        # 保护：如果"最近一次起床"距离现在还不够久（默认10小时），不能让同一个sleep_time
        # 目标点把人反复拽回睡眠——比如昨晚23点睡到早上8点起床，下午管理员只要安静一会儿，
        # target_dt(昨天23点)依然是"过去的时间"，now >= target_dt 对今天剩下的每一刻都成立。
        # 用时间间隔而不是单纯比较日期字符串，是为了兼容熬夜场景：凌晨3点睡5点起床后，
        # 当晚23点应该能正常入睡，这时候距离上次起床已经过了18小时，不会被这个保护挡住。
        last_wake_ts = self.state.get("last_wake_ts")
        min_gap_hours = float(self.cfg.get("min_hours_since_wake_before_resleep", 10))
        if last_wake_ts and (time.time() - last_wake_ts) < min_gap_hours * 3600:
            return
        target = self._parse_time(self._get_day_sleep_time(now))
        offset = random.randint(-int(self.cfg.get("sleep_variance", 30)), int(self.cfg.get("sleep_variance", 30)))
        target_dt = now.replace(hour=target.hour, minute=target.minute, second=0) + timedelta(minutes=offset)
        if now < target_dt:
            return
        admin_idle     = (time.time() - self._last_admin_ts) >= int(self.cfg.get("admin_idle_minutes", 30)) * 60
        private_silent = self._is_private_silent()

        if admin_idle and private_silent:
            self.state["presleep_pending"] = False
            self._save_state()
            await self._enter_sleep()
        elif admin_idle and not private_silent:
            # ②满足③不满足：进入"准备入睡"状态，让别人的消息被注入结束话题提示词，
            # 同时只触发一次延迟入睡的倒计时
            if not self.state.get("presleep_pending"):
                self.state["presleep_pending"] = True
                self._save_state()
                asyncio.create_task(self._delayed_sleep(int(self.cfg.get("others_grace_minutes", 3)) * 60))
        elif not admin_idle:
            # ①满足但管理员还在聊（②不满足）：不会去睡，但主动跟管理员道一声晚安（只说一次）
            if not self.state.get("goodnight_said_today"):
                self.state["goodnight_said_today"] = True
                self._save_state()
                p = self.cfg.get("sleep_admin_goodnight_prompt", "")
                if p:
                    msg = await self._generate_proactive_message(p)
                    if msg:
                        await self._send_to_admin(msg)

    async def _check_wake(self, now: datetime):
        sleep_start = self.state.get("sleep_start")
        if sleep_start:
            min_sleep_sec = int(self.cfg.get("min_sleep_seconds_before_wake", 600))
            if time.time() - sleep_start < min_sleep_sec:
                return  # 兜底保护：不管是哪条判断路径触发的，入睡没多久绝不应该被判定该起床
        special = self._check_special_wake(now)
        if special:
            msg = await self._generate_proactive_message(self.cfg.get("prompts_special_wake", ""))
            await self._do_wake_up(special_prompt=msg)
            return
        wake_str = self.state.get("custom_wake_time") or self._get_day_wake_time(now)
        target   = self._parse_time(wake_str)
        offset   = random.randint(0, int(self.cfg.get("wake_variance", 20)))
        target_dt = now.replace(hour=target.hour, minute=target.minute, second=0) + timedelta(minutes=offset)
        if now >= target_dt:
            await self._do_wake_up()

    def _check_special_wake(self, now: datetime) -> bool:
        """
        特殊起床场景：比如管理员要上学/上班，到点必须醒，不受正常作息随机波动影响。
        只在 [起床时间, 结束时间] 这个窗口内才会触发，且当天只触发一次，
        避免窗口期之外（比如下午、晚上）误判导致反复强制唤醒。
        """
        if not self.cfg.get("special_wake_enabled", False):
            return False
        if self.state.get("special_wake_done_today"):
            return False  # 今天已经触发过，不再重复
        days_raw = str(self.cfg.get("special_wake_days", "1,2,3,4,5"))
        # 配置里 1=周一...7=周日，Python weekday() 是 0=周一...6=周日，需要+1对齐
        active_days = {int(d.strip()) for d in days_raw.split(",") if d.strip().isdigit()}
        if (now.weekday() + 1) not in active_days:
            return False
        wake_target = self._parse_time(self.cfg.get("special_wake_time", "06:35"))
        online_end  = self._parse_time(self.cfg.get("special_wake_end", "07:30"))
        wake_dt   = now.replace(hour=wake_target.hour, minute=wake_target.minute, second=0)
        online_dt = now.replace(hour=online_end.hour, minute=online_end.minute, second=0)
        if not online_dt > wake_dt:
            online_dt += timedelta(days=1)  # 跨午夜的窗口（比如23:50~00:30），结束时间要算到第二天
        # 只在 [起床时间, 结束时间] 这个窗口内才算命中，窗口外（比如下午、晚上）不触发
        if wake_dt <= now <= online_dt:
            self.state["special_wake_done_today"] = True
            self._save_state()
            return True
        return False

    async def _check_nap(self, now: datetime):
        if not self.cfg.get("nap_enabled", True):
            return
        cooldown_until = self.state.get("nap_cooldown_until", 0)
        if cooldown_until and time.time() < cooldown_until:
            return  # 刚被吵醒不久，冷却期内不重新判定午休/补觉，避免秒睡秒醒的死循环
        last_dur  = self.state.get("last_sleep_duration", 28800)
        critical  = int(self.cfg.get("nap_critical_hours", 2)) * 3600
        min_hours = int(self.cfg.get("nap_min_sleep_hours", 6)) * 3600

        # "严重睡眠不足，随时可以补觉"这个判定，不能只看last_sleep_duration这一个数字
        # （新装插件、数据异常时都可能让这个数字不可靠）。需要同时满足：
        # ①数值确实低于阈值 ②昨晚有实际记录在案的熬夜/短睡眠，或者当前正连续熬夜中
        # 否则只走普通的"午休窗口"判断，不允许无视时间段随时触发。
        is_crit = False
        if last_dur < critical:
            recent_short_sleep = False
            finished_logs = [e for e in self.daily_log if e.get("wake_time")]
            if finished_logs:
                last_log = finished_logs[-1]
                last_log_dur = last_log.get("duration_seconds") or last_dur
                recent_short_sleep = last_log_dur < critical
            is_overnight_now = self.state.get("consecutive_overtime", 0) > 0
            is_crit = recent_short_sleep or is_overnight_now

        in_window = is_crit
        if not is_crit:
            ws = self._parse_time(self.cfg.get("nap_window_start", "13:00"))
            we = self._parse_time(self.cfg.get("nap_window_end",   "16:00"))
            in_window = self._in_range(now.time(), ws, we)
        if in_window and (self.cfg.get("siesta_enabled", True) or last_dur < min_hours):
            if random.random() < float(self.cfg.get("nap_probability", 0.8)) and self._is_private_silent():
                await self._enter_nap(is_critical=is_crit)

    async def _check_nap_wake(self, now: datetime):
        """
        午休/补觉的自然结束判断（不依赖被吵醒）：
        - 普通午休：按时间窗口和随机概率触发，到了 nap_window_end 就该自然醒，
          因为"午休"本身是一个时间段的概念，不是"睡够某个时长"的概念。
        - 严重补觉：目标是补够缺的睡眠，按 nap_min_sleep_hours 算出还需要睡多久，
          睡够了就自然醒，不需要等被吵。
        """
        nap_start = self.state.get("nap_start")
        if not nap_start:
            return
        elapsed = time.time() - nap_start
        is_critical = self.state.get("nap_is_critical", False)

        if is_critical:
            min_hours = float(self.cfg.get("nap_min_sleep_hours", 6))
            last_dur  = self.state.get("last_sleep_duration", 0)
            target_seconds = max(0, min_hours * 3600 - last_dur)
            offset_min = self.state.get("nap_end_offset_min", 0)
            target_seconds += offset_min * 60
            # 至少给15分钟意思一下，避免昨晚只差几秒钟也要补觉这种荒谬情况
            target_seconds = max(target_seconds, 900)
            if elapsed >= target_seconds:
                await self._finish_nap(reason="补够了")
        else:
            we = self._parse_time(self.cfg.get("nap_window_end", "16:00"))
            offset_min = self.state.get("nap_end_offset_min", 0)
            window_end_dt = now.replace(hour=we.hour, minute=we.minute, second=0, microsecond=0) + timedelta(minutes=offset_min)
            nap_start_dt = self._local_dt(nap_start)
            if window_end_dt > nap_start_dt:
                # 正常情况：入睡时间在窗口结束之前，到了窗口结束点就该醒
                if now >= window_end_dt:
                    await self._finish_nap(reason="午休时间到了")
            else:
                # 入睡时间已经晚于窗口结束点（比如窗口外触发的午休），
                # 用一个固定的最长时长保护，而不是死板比较已经过去的窗口时间点，
                # 否则会出现"一入睡就立刻判定该结束"的荒谬情况
                max_minutes = int(self.cfg.get("nap_max_duration_minutes", 90))
                if elapsed >= max_minutes * 60:
                    await self._finish_nap(reason="午休时长已到上限")

    async def _finish_nap(self, reason: str = ""):
        """午休/补觉自然结束（不是被吵醒），更新睡眠时长统计，恢复清醒。"""
        nap_start = self.state.get("nap_start")
        if nap_start:
            nap_duration = time.time() - nap_start
            # 补觉算到睡眠时长里，让后续判断知道已经补过了，不会马上又判定"严重不足"
            self.state["last_sleep_duration"] = self.state.get("last_sleep_duration", 0) + nap_duration
        self.state["nap_start"] = None
        self.state["nap_is_critical"] = False
        # 关键：也要更新last_wake_ts！否则_check_sleep那边的保护形同虚设——它判断的是
        # "距离上次起床多久"，如果这里不更新，它看到的还是更早之前那次真正起床的时刻，
        # 一旦那次起床已经过了保护期，补觉刚结束就会立刻又被_check_sleep拽进新一轮睡眠，
        # 走完整的入睡-起床流程，连带触发报告推送——这才是真正的根因
        self.state["last_wake_ts"] = time.time()
        self._set_state(S.AWAKE)
        self._emit_state("awake")
        self._log("info", f"午休/补觉自然结束（{reason}）")


    async def _check_resleep(self, now: datetime):
        """
        二次入睡：被吵醒后，如果管理员和别人都安静下来了，就重新睡回去。
        ①管理员无消息满 resleep_admin_idle_minutes 是必要条件，不满足就不会二次入睡。
        ②别人无消息满 resleep_others_idle_minutes 才能立刻睡；不满足时先注入"准备睡了"的
          过渡提示词，等 resleep_grace_minutes 后再真正关闭（避免一说要睡就秒断）。
        """
        if not self.cfg.get("resleep_enabled", True) or not self.state.get("resleep_pending"):
            return
        cooldown_until = self.state.get("resleep_cooldown_until", 0)
        if cooldown_until and time.time() < cooldown_until:
            return  # 刚被吵醒不久，冷却期内不重新判定二次入睡，避免秒睡秒醒
        # 已经到了正常起床时间附近就不再二次入睡，避免和正常作息冲突
        wake_str = self._get_day_wake_time(now)
        wake_t = self._parse_time(wake_str)
        if self._in_range(now.time(), wake_t, (now + timedelta(minutes=30)).time()):
            self.state["resleep_pending"] = False
            self._save_state()
            # 这里才是真正确定"今晚不会再二次入睡"的时刻，daily_log记录在这里收尾，
            # 之前一直保持"未结束"状态正是为了让前面的二次入睡能延续记录而不是另开一条
            self._close_daily_log()
            return

        admin_idle_minutes = int(self.cfg.get("resleep_admin_idle_minutes", 10))
        others_idle_minutes = int(self.cfg.get("resleep_others_idle_minutes", 8))

        # 跨时区补觉偏好：管理员吵醒 + 时区偏移较大时，缩短等待时间（更自然地补回睡眠）
        if self.cfg.get("timezone_enabled", False) and abs(float(self.cfg.get("timezone_offset_hours", 0))) >= 4:
            boost = float(self.cfg.get("timezone_sleep_nap_boost", 0.3))
            if boost > 0:
                admin_idle_minutes = max(3, int(admin_idle_minutes * (1 - boost)))
                others_idle_minutes = max(3, int(others_idle_minutes * (1 - boost)))

        admin_idle_ok = (time.time() - self._last_admin_ts) >= admin_idle_minutes * 60
        if not admin_idle_ok:
            return  # ①不满足，管理员还在聊，完全不进入二次入睡流程

        others_idle_ok = self._is_private_silent(others_idle_minutes * 60)
        if others_idle_ok:
            self.state["resleep_pending"] = False
            self._save_state()
            await self._enter_sleep()
            self._log("info", "二次入睡")
        elif not self.state.get("resleep_grace_started"):
            # 别人还在说话：先过渡，再等 grace 时间关闭
            self.state["resleep_grace_started"] = True
            self._save_state()
            asyncio.create_task(self._delayed_resleep(int(self.cfg.get("resleep_grace_minutes", 3)) * 60))

    async def _delayed_resleep(self, delay: float):
        await asyncio.sleep(delay)
        self.state["resleep_grace_started"] = False
        if self.state.get("resleep_pending") and self.state["sleep_state"] not in (S.SLEEPING, S.NAPPING):
            self.state["resleep_pending"] = False
            self._save_state()
            await self._enter_sleep()
            self._log("info", "二次入睡（grace超时后）")
        else:
            self._save_state()

    async def _check_overtime_decision(self, now: datetime):
        if not self.cfg.get("overtime_enabled", True) or self.state.get("overtime_decided_today"):
            return
        if self.state["sleep_state"] == S.SLEEPING:  # 已经睡着了，不该再问要不要熬夜
            return
        t  = self._parse_time(self._get_day_sleep_time(now))
        td = now.replace(hour=t.hour, minute=t.minute, second=0)
        if not (td - timedelta(minutes=30) <= now <= td):
            return
        mode = self.cfg.get("overtime_mode", "probability")
        woken_today = self.state.get("woken_by_others_today", [])
        # 今天被别人吵醒过：略微提高熬夜概率，模拟“没睡好/不甘心，索性熬一会”的心理
        bump = min(0.15, 0.05 * len(woken_today))
        should = (random.random() < float(self.cfg.get("overtime_probability", 0.1)) + bump) if mode == "probability" \
                 else (self.state.get("weekly_overtime", 0) < int(self.cfg.get("overtime_weekly_limit", 2)))
        if should:
            self.state["overtime_decided_today"] = True
            self._save_state()
            base_prompt = self.cfg.get("overtime_ask_prompt", "今天晚上，你想熬夜吗？由你自己来决定，只回复是或否。")
            if woken_today:
                base_prompt += f"\n（补充信息：今天你白天被{' / '.join(woken_today)}吵醒过 {len(woken_today)} 次，可以结合心情自行决定）"
            await self._send_to_admin(base_prompt)
            if self.cfg.get("weekend_auto_adjust_enabled", True):
                note = self.cfg.get("prompts_overtime_ask_later_wake", "熬夜了")
                p = self.cfg.get("prompts_weekend_ask", "").replace("{overtime_note}", note)
                if p:
                    asyncio.create_task(self._delayed_send_to_admin(p, delay=10))

    async def _check_weekend_adjust(self, now: datetime):
        """即使没熬夜，第二天是周末/假期时，也允许 AI 自主决定晚点起床（每天只问一次）。"""
        if not self.cfg.get("weekend_auto_adjust_enabled", True):
            return
        if self.state.get("weekend_asked_today"):
            return
        # 简单判定：明天是周六/周日就算"周末"（节假日需用户在作息表里手动配置，这里只兜底周末场景）
        tomorrow = now + timedelta(days=1)
        if tomorrow.weekday() not in (5, 6):
            return
        t  = self._parse_time(self._get_day_sleep_time(now))
        td = now.replace(hour=t.hour, minute=t.minute, second=0)
        if not (td - timedelta(minutes=20) <= now <= td):
            return
        self.state["weekend_asked_today"] = True
        self._save_state()
        p = self.cfg.get("prompts_weekend_ask", "").replace("{overtime_note}", "")
        if p:
            await self._send_to_admin(p)

    async def _delayed_send_to_admin(self, content: str, delay: float = 10):
        await asyncio.sleep(delay)
        await self._send_to_admin(content)

    async def _check_nightmare(self):
        if not self.cfg.get("nightmare_enabled", True) or self.state.get("nightmare_tonight"):
            return
        if random.random() < float(self.cfg.get("nightmare_probability", 0.02)) / 60:
            self.state["nightmare_tonight"] = True
            self._save_state()
            p = self.cfg.get("nightmare_prompt", "你刚才做噩梦惊醒了，现在心里有点难受，可以发消息给管理员寻求安慰。")
            msg = await self._generate_proactive_message(p)
            if msg:
                await self._send_to_admin(msg)
            self._set_state(S.AWAKE)
            self._emit_state("awake")

    async def _alarm_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_alarms()
            except Exception as e:
                logger.error(f"[sleep_sense] alarm error: {e}")

    async def _check_alarms(self):
        now, done = datetime.now(), []
        for a in self.alarms:
            try:
                alarm_dt = datetime.strptime(f"{a['date']} {a['time']}", "%Y-%m-%d %H:%M")
            except Exception:
                done.append(a); continue
            if now >= alarm_dt:
                done.append(a)
                p = f"闹钟提醒：{a.get('reason','')}。你被这个闹钟唤醒了，自行判断现在的时间，正常和管理员打个招呼或者说明情况。"
                msg = await self._generate_proactive_message(p)
                if msg:
                    await self._send_to_admin(msg)
                self._log("info", f"闹钟触发: {a}")
        self.alarms = [x for x in self.alarms if x not in done]
        if done:
            self._save_json(ALARMS_PATH, self.alarms)

    # ═══════════════════════════════════════════════════════════════════
    # 状态转换
    # ═══════════════════════════════════════════════════════════════════
    async def _enter_sleep(self):
        is_resleep = self.state.get("resleep_pending", False)
        self._set_state(S.SLEEPING)
        now_ts = time.time()
        self.state.update({"sleep_start": now_ts, "sleep_cycle_phase": "light",
                           "nightmare_tonight": False, "overtime_decided_today": False,
                           "weekend_asked_today": False, "woken_by_others_today": [],
                           "special_wake_done_today": False, "overtime_since": None})
        # 只有"全新一晚"的首次入睡才清零累计时长；二次入睡是接着前面没睡够的部分继续，
        # 不能清零，否则前面被吵醒之前睡的那段会直接丢失（这正是"按上一段算"那个bug的根因）
        if not is_resleep:
            self.state["last_sleep_duration"] = 0
        self._msg_counters.clear(); self._woken_msg_count.clear()
        self._dream_generated_tonight = False
        self._cycle_plan = self._generate_cycle_plan(now_ts)
        self._save_state()
        self._emit_state("sleep")
        self._log("info", "进入睡眠" if not is_resleep else "二次入睡")
        self._record_stat("sleep_start", datetime.now().isoformat())
        self._open_daily_log(now_ts)
        asyncio.create_task(self._dream_scheduler())

    async def _enter_nap(self, is_critical: bool = False):
        self._set_state(S.NAPPING)
        self.state["nap_start"] = time.time()
        self.state["nap_is_critical"] = is_critical
        variance = int(self.cfg.get("nap_end_variance", 15))
        self.state["nap_end_offset_min"] = random.randint(-variance, variance) if variance > 0 else 0
        self._dream_generated_tonight = False
        # 清空上一次午休/补觉残留的吵醒计数余量，避免这次刚开始没多久就被"凑数"吵醒
        for k in list(self._msg_counters.keys()):
            if k.startswith("nap_"):
                del self._msg_counters[k]
        self._save_state()
        self._emit_state("nap")
        self._log("info", f"进入{'严重补觉' if is_critical else '午休'}")
        asyncio.create_task(self._nap_dream_scheduler())

    async def _do_wake_up(self, special_prompt: str = ""):
        start = self.state.get("sleep_start")
        if start:
            actual_duration = time.time() - start
            # 保护：睡眠时长异常短（小于5分钟），很可能是配置错误或异常触发导致的"秒起"，
            # 不要用这个污染last_sleep_duration，否则后续会一直误判"严重睡眠不足"导致反复午休
            if actual_duration >= 300:
                # 累加而不是覆盖：如果这一晚经历过"睡→被吵醒→二次入睡→醒"，
                # 前面那段时长已经在二次入睡时保留在last_sleep_duration里了，这里要接着加，
                # 不能直接覆盖，否则只会统计到最后一段，前面睡的时间全部丢失
                self.state["last_sleep_duration"] = self.state.get("last_sleep_duration", 0) + actual_duration
            else:
                self._log("warn", f"本次睡眠时长异常短({actual_duration:.0f}秒)，不计入睡眠时长统计，避免影响后续判断")
        self.state["custom_wake_time"]    = None
        self.state["consecutive_overtime"] = 0
        self.state["goodnight_said_today"] = False
        self.state["presleep_pending"]     = False
        # 记录"最近一次起床"的时刻，配合_check_sleep里的保护，避免在起床后不久
        # 又被同一个sleep_time目标点拽回睡眠（用间隔小时数判断，兼容熬夜场景）
        self.state["last_wake_ts"] = time.time()
        self._set_state(S.AWAKE)
        self._emit_state("awake")
        self._log("info", "正常起床" if not special_prompt else "特殊起床")
        self._record_stat("wake_time", datetime.now().isoformat())

        # 在收尾daily_log之前，先取出"昨晚谁找过他但没吵醒成功"的记录，
        # 醒来后主动回应这些人，而不是装作什么都不知道
        attempted_contacts = []
        if self.daily_log and self.daily_log[-1].get("wake_time") is None:
            attempted_contacts = self.daily_log[-1].get("attempted_contacts", [])

        self._close_daily_log()
        if special_prompt:
            msg = await self._generate_proactive_message(special_prompt)
            if msg:
                await self._send_to_admin(msg)
        await self._dream_recall_on_wake()

        if attempted_contacts and self.cfg.get("wake_check_missed_contacts", True):
            await self._notify_missed_contacts(attempted_contacts)

        # 早安通知
        if self.cfg.get("morning_greeting_enabled", True):
            p = self.cfg.get("prompts_morning_greeting", "")
            if p:
                msg = await self._generate_proactive_message(p)
                if msg:
                    await self._send_to_admin(msg)
        # 晚安报告图片自动推送：一天只推送一次，不再额外限定"必须是早晨这个时间段"。
        # 之前加的时间窗口判断引入了不必要的复杂度（配置解析、跨午夜处理等），
        # 反而成了新bug的来源。简单的"按天去重"已经能避免反复刷屏，足够了。
        today_str = self._local_now().strftime("%Y-%m-%d")
        if (self.cfg.get("sleep_report_enabled", True) and self.cfg.get("sleep_report_auto_push", True)
                and self.state.get("report_pushed_date") != today_str):
            self.state["report_pushed_date"] = today_str
            self._save_state()
            asyncio.create_task(self._push_sleep_report())

    async def _notify_missed_contacts(self, contacts: list):
        """
        醒来后，如果昨晚有人找过他但没成功吵醒，给管理员提一句，
        让AI知道这件事、决定要不要主动去回应（比较像真人睡醒翻消息的感觉）。
        管理员自己找过他的不用提（管理员自己知道），主要是提醒"别人找过你"这种情况。
        """
        others_contacts = [c for c in contacts if not c.get("is_admin")]
        if not others_contacts:
            return
        descs = []
        for c in others_contacts[:5]:  # 最多提5个，避免消息太长
            scene = "群里" if c.get("is_group") else "私聊"
            descs.append(f"{scene}有人找过你{c.get('count',1)}次（{c.get('first_time','')}左右）")
        summary = "；".join(descs)
        prompt = self.cfg.get(
            "prompts_missed_contacts",
            "你刚睡醒，翻了一下消息记录，发现昨晚睡着的时候：{summary}。"
            "可以自己决定要不要去看看回应一下，不是很重要的话也可以不用管。"
        ).replace("{summary}", summary)
        msg = await self._generate_proactive_message(prompt)
        if msg:
            await self._send_to_admin(msg)

    async def _push_sleep_report(self):
        """起床后渲染并推送晚安报告图片给管理员。"""
        last_push = self.state.get("last_report_push_ts", 0)
        min_interval = int(self.cfg.get("report_min_interval_minutes", 15)) * 60
        if last_push and time.time() - last_push < min_interval:
            self._log("warn", "晚安报告推送间隔太短，跳过这次（防止异常情况下刷屏）")
            return
        finished = [e for e in self.daily_log if e.get("wake_time")]
        if not finished:
            return
        log = finished[-1]
        overlap_h = self._calc_overlap_hours()
        overlap_hint = ""
        if overlap_h is not None and overlap_h > 0:
            overlap_hint = f"你们今天醒着的重叠时间大约 {overlap_h} 小时"
        elif overlap_h == 0:
            overlap_hint = "今天时差太大，醒着的时间几乎没有重叠"
        img_path = await asyncio.get_event_loop().run_in_executor(
            None, self._render_sleep_report, log, overlap_hint
        )
        if not img_path:
            return
        self.state["last_report_push_ts"] = time.time()
        self._save_state()
        try:
            from astrbot.api.event import MessageChain
            import astrbot.api.message_components as Comp
            chain = MessageChain().file_image(img_path)
            umo = self.state.get("admin_umo")
            if umo:
                await self.context.send_message(umo, chain)
        except Exception as e:
            self._log("warn", f"发送报告图片失败: {e}")

    def _set_state(self, s: str):
        self.state["sleep_state"] = s
        self._save_state()

    def _emit_state(self, s: str):
        try:
            self.context.emit_event("sleep_plugin_state_change", {"state": s})
        except Exception:
            pass

    async def _delayed_sleep(self, delay: float):
        await asyncio.sleep(delay)
        self.state["presleep_pending"] = False
        if self.state["sleep_state"] not in (S.SLEEPING, S.NAPPING):
            await self._enter_sleep()
        else:
            self._save_state()

    # ═══════════════════════════════════════════════════════════════════
    # 睡眠周期（多轮随机波动，模拟真人一整晚反复浅睡/深睡的过程）
    # ═══════════════════════════════════════════════════════════════════
    def _generate_cycle_plan(self, sleep_start_ts: float) -> list[dict]:
        """
        预先生成一整晚的周期轨迹表（一次性算好，不是边睡边随机决定，方便存档回看）。
        结构：多轮周期（每轮 70~110 分钟，随机），每轮内部 浅睡→深睡→浅睡。
        越往后半夜，深睡占比越低（更接近真实睡眠后段变浅的规律）。
        返回 [{"phase": "light"/"deep", "start_min": 0, "end_min": 18}, ...]（单位：入睡后第几分钟）
        """
        if not self.cfg.get("sleep_cycle_enabled", True):
            return [{"phase": "normal", "start_min": 0, "end_min": 24 * 60}]

        round_min = int(self.cfg.get("cycle_round_min_minutes", 70))
        round_max = int(self.cfg.get("cycle_round_max_minutes", 110))
        ratio_first = float(self.cfg.get("cycle_deep_ratio_first", 0.45))
        ratio_last  = float(self.cfg.get("cycle_deep_ratio_last", 0.15))

        total_minutes = 11 * 60  # 预生成够长(11小时)的轨迹，醒来时无论几点都有覆盖
        plan: list[dict] = []
        cursor = 0
        round_idx = 0
        max_rounds = 8  # 安全上限，避免极端配置死循环

        while cursor < total_minutes and round_idx < max_rounds:
            round_len = random.randint(round_min, round_max)
            # 深睡占比随轮次推进从 ratio_first 线性过渡到 ratio_last
            progress = min(1.0, round_idx / 4.0)
            deep_ratio = ratio_first + (ratio_last - ratio_first) * progress
            deep_ratio = max(0.05, min(0.7, deep_ratio + random.uniform(-0.05, 0.05)))

            deep_len = max(5, round(round_len * deep_ratio))
            edge_len = max(5, (round_len - deep_len) // 2)  # 前后浅睡段
            light1_len = edge_len
            light2_len = max(5, round_len - deep_len - light1_len)

            for phase, length in (("light", light1_len), ("deep", deep_len), ("light", light2_len)):
                if length <= 0:
                    continue
                plan.append({"phase": phase, "start_min": cursor, "end_min": cursor + length})
                cursor += length
            round_idx += 1

        if not plan:
            plan = [{"phase": "light", "start_min": 0, "end_min": total_minutes}]
        return plan

    def _update_cycle(self):
        if not self.cfg.get("sleep_cycle_enabled", True):
            self.state["sleep_cycle_phase"] = "normal"
            return
        plan = getattr(self, "_cycle_plan", None)
        if not plan:
            plan = self._generate_cycle_plan(self.state.get("sleep_start") or time.time())
            self._cycle_plan = plan

        elapsed = (time.time() - (self.state.get("sleep_start") or time.time())) / 60
        phase = "normal"
        for seg in plan:
            if seg["start_min"] <= elapsed < seg["end_min"]:
                phase = seg["phase"]
                break
        else:
            if plan:
                phase = plan[-1]["phase"]

        # 深睡期里随机短暂"回浅睡"的小波动，更接近真人睡眠不是绝对平稳的
        if phase == "deep":
            wobble_p = float(self.cfg.get("cycle_wobble_probability", 0.08))
            if random.random() < wobble_p:
                phase = "light"

        prev = self.state.get("sleep_cycle_phase", "normal")
        self.state["sleep_cycle_phase"] = phase
        if phase != prev:
            self._mark_cycle_phase(phase)

    def _get_cycle_multiplier(self) -> float:
        if not self.cfg.get("sleep_cycle_enabled", True):
            return 1.0
        phase = self.state.get("sleep_cycle_phase", "normal")
        if phase == "light": return float(self.cfg.get("cycle_light_multiplier", 0.5))
        if phase == "deep":  return float(self.cfg.get("cycle_deep_multiplier",  1.5))
        return 1.0

    def _get_cycle_prompt_prefix(self) -> str:
        if not self.cfg.get("sleep_cycle_enabled", True):
            return ""
        # 刚躺下没多久就被吵醒：用专门的提示词，避免"才睡5分钟就说很困很重"的违和感
        sleep_start = self.state.get("sleep_start")
        if sleep_start:
            elapsed_min = (time.time() - sleep_start) / 60
            just_fell = int(self.cfg.get("cycle_just_fell_asleep_minutes", 10))
            if elapsed_min < just_fell:
                return self.cfg.get("prompts_just_fell_asleep", "") + " "
        phase = self.state.get("sleep_cycle_phase", "normal")
        if phase == "light": return self.cfg.get("prompts_cycle_light", "") + " "
        if phase == "deep":  return self.cfg.get("prompts_cycle_deep",  "") + " "
        return ""

    # ═══════════════════════════════════════════════════════════════════
    # 做梦引擎
    # ═══════════════════════════════════════════════════════════════════
    def _collect_dream_ctx(self, text: str):
        max_ctx = int(self.cfg.get("dream_ctx_max", 30))
        stripped = text.strip()
        if len(stripped) >= 4:
            self._pre_sleep_ctx.append(stripped)
            if len(self._pre_sleep_ctx) > max_ctx:
                self._pre_sleep_ctx = self._pre_sleep_ctx[-max_ctx:]

    async def _dream_scheduler(self):
        if not self.cfg.get("dream_enabled", True):
            return
        # 等到差不多第一轮深睡期附近再考虑生成梦境（新的周期模型里没有固定"深睡开始时间"，
        # 用每轮平均时长的中点估算）
        round_min = int(self.cfg.get("cycle_round_min_minutes", 70))
        round_max = int(self.cfg.get("cycle_round_max_minutes", 110))
        deep_min = (round_min + round_max) // 2
        trigger  = random.randint(0, int(self.cfg.get("dream_window_minutes", 60))) * 60
        await asyncio.sleep(deep_min * 60 + trigger)
        if self.state["sleep_state"] != S.SLEEPING or self._dream_generated_tonight:
            return
        if random.random() > float(self.cfg.get("dream_probability", 0.7)):
            return
        await self._generate_dream()

    async def _nap_dream_scheduler(self):
        """
        午休/补觉专用的梦境调度，等待时间比夜间短很多（毕竟午休通常没那么久），
        概率也单独配置（默认比夜间低一些，因为短时间小睡不一定能做梦）。
        """
        if not self.cfg.get("dream_enabled", True) or not self.cfg.get("nap_dream_enabled", True):
            return
        wait_min = int(self.cfg.get("nap_dream_wait_minutes", 15))
        await asyncio.sleep(wait_min * 60)
        if self.state["sleep_state"] != S.NAPPING or self._dream_generated_tonight:
            return
        if random.random() > float(self.cfg.get("nap_dream_probability", 0.3)):
            return
        await self._generate_dream()

    async def _generate_dream(self):
        if self._dream_generated_tonight:
            return
        self._dream_generated_tonight = True

        ctx_lines   = self._pre_sleep_ctx[-int(self.cfg.get("dream_ctx_use", 15)):]
        ctx_summary = "、".join(ctx_lines) if ctx_lines else "（没有特别的内容）"
        dream_type  = self._pick_dream_type()

        gen_prompt = self.cfg.get("dream_generate_prompt", "").strip()
        if not gen_prompt:
            gen_prompt = (
                "你现在正在睡觉，进入了做梦状态。\n"
                "今天睡前聊到的内容大致是：{ctx}\n"
                "梦的类型是：{type}\n"
                "请用第一人称，生成一段梦境。要求：\n"
                "- 意象自由联想，不需要逻辑连贯，允许跳跃和隐喻\n"
                "- 100~200字，像碎片一样\n"
                "- 直接输出梦境内容，不要有任何前缀或解释"
            )
        gen_prompt = gen_prompt.replace("{ctx}", ctx_summary).replace("{type}", dream_type)

        dream_content = await self._llm_generate(gen_prompt)
        if not dream_content:
            self._log("warn", "梦境生成失败")
            return

        will_recall = random.random() < float(self.cfg.get("dream_recall_probability", 0.6))
        if will_recall:
            clarity = random.choices(
                ["clear", "blurry", "feeling_only"],
                weights=[int(self.cfg.get("dream_clarity_clear_weight", 40)),
                         int(self.cfg.get("dream_clarity_blurry_weight", 40)),
                         int(self.cfg.get("dream_clarity_feeling_weight", 20))]
            )[0]
        else:
            clarity = "forgotten"

        period = "night" if self.state.get("sleep_state") == S.SLEEPING else "day"
        record = {"date": datetime.now().strftime("%Y-%m-%d"), "sleep_start": self.state.get("sleep_start"),
                  "type": dream_type, "content": dream_content, "clarity": clarity,
                  "recalled": will_recall, "ts": time.time(), "period": period}
        self.dreams.append(record)
        max_archive = int(self.cfg.get("dream_archive_max", 30))
        if len(self.dreams) > max_archive:
            self.dreams = self.dreams[-max_archive:]
        self._save_json(DREAMS_PATH, self.dreams)
        self.state["tonight_dream"] = {"content": dream_content, "type": dream_type, "clarity": clarity, "recalled": will_recall}
        self._save_state()
        self._log("info", f"梦境已生成 type={dream_type} clarity={clarity}")

    def _pick_dream_type(self) -> str:
        types = [
            ("日常碎片", int(self.cfg.get("dream_type_w_daily", 35))),
            ("奇异梦境", int(self.cfg.get("dream_type_w_strange", 25))),
            ("美梦",     int(self.cfg.get("dream_type_w_sweet", 20))),
            ("噩梦",     int(self.cfg.get("dream_type_w_nightmare", 10))),
            ("情感梦",   int(self.cfg.get("dream_type_w_emotion", 10))),
        ]
        extra = self.cfg.get("dream_extra_types", "")
        if extra:
            w = int(self.cfg.get("dream_extra_types_weight", 5))
            for t in str(extra).split(","):
                t = t.strip()
                if t: types.append((t, w))
        return random.choices([t[0] for t in types], weights=[max(1, t[1]) for t in types])[0]

    async def _dream_recall_on_wake(self):
        if not self.cfg.get("dream_enabled", True) or not self.cfg.get("dream_recall_on_wake", True):
            return
        dream = self.state.pop("tonight_dream", None)
        if not dream:
            return
        self._save_state()
        clarity = dream.get("clarity", "forgotten")
        content = dream.get("content", "")
        dtype   = dream.get("type", "")
        if clarity == "forgotten":
            return
        if clarity == "clear":
            p = self.cfg.get("dream_recall_clear_prompt", "").strip() or (
                "你刚刚睡醒，还记得昨晚做的梦。梦的内容是：{dream}\n"
                "你可以自然地跟管理员提一下，就像真人早上说「我昨晚做了个奇怪的梦」，只说印象最深的片段，语气随意。")
        elif clarity == "blurry":
            p = self.cfg.get("dream_recall_blurry_prompt", "").strip() or (
                "你刚刚睡醒，隐约记得昨晚做了梦，但细节模糊了。梦大概是关于：{dream}\n"
                "你可以提一句，说「好像梦到了什么，但记不太清了」，语气随意，可以有点困。")
        else:
            p = self.cfg.get("dream_recall_feeling_prompt", "").strip() or (
                "你刚刚睡醒，梦已经记不住了，只剩下一点模糊的感觉。梦的类型是{type}。"
                "你可以说一句「做了个梦但完全记不住了」，或者说说醒来的感觉。")
        full_prompt = p.replace("{dream}", content).replace("{type}", dtype)
        msg = await self._generate_proactive_message(full_prompt)
        if msg:
            await self._send_to_admin(msg)
        self._log("info", f"梦境浮现 clarity={clarity}")

    async def _llm_generate(self, prompt: str) -> str:
        try:
            dream_provider_id = self.cfg.get("dream_provider_id", "").strip()
            provider = None
            if dream_provider_id:
                try:
                    provider = self.context.get_provider_by_id(dream_provider_id)
                except Exception as e:
                    self._log("warn", f"指定的梦境模型 {dream_provider_id} 获取失败，回退到默认模型: {e}")
            if not provider:
                provider = self.context.get_using_provider()
            if provider:
                resp = await provider.text_chat(prompt=prompt, session_id="sleep_sense_dream_internal")
                if resp and resp.completion_text:
                    return resp.completion_text.strip()
        except Exception as e:
            self._log("warn", f"LLM 生成梦境失败: {e}")
        return ""

    # ═══════════════════════════════════════════════════════════════════
    # 工具方法
    # ═══════════════════════════════════════════════════════════════════
    def _get_msg_scope_id(self, event, uid: str, is_group: bool) -> str:
        """
        构造一个能精确区分消息来源的key：
        - 私聊：用uid就够了（同一个人的私聊只有一个会话）
        - 群聊：必须加上群号，否则同一个人在A群发1次、B群发1次，会被错误地合并计成2次，
          导致"多个群聊各艾特一次就能凑够吵醒阈值"这种不合理情况。
        """
        if not is_group:
            return f"p_{uid}"
        try:
            gid = event.get_group_id() or "unknown_group"
        except Exception:
            gid = "unknown_group"
        return f"g_{gid}_{uid}"

    def _is_admin(self, uid: str) -> bool:
        return (self.cfg.get("admin_enabled", True)
                and str(uid) == str(self.cfg.get("admin_qq", "")))

    def _is_private_silent(self, thresh_sec: int = 300) -> bool:
        now = time.time()
        return all(now - ts >= thresh_sec for ts in self._last_others_ts.values())

    # ═══════════════════════════════════════════════════════════════════
    # 晚安报告图片渲染（Sanrio可爱风：粉紫渐变 + 圆角卡片 + 睡眠轨迹）
    # ═══════════════════════════════════════════════════════════════════
    def _render_sleep_report(self, log: dict, overlap_hint: str = "") -> str | None:
        """渲染晚安报告图片，支持多主题和模块开关。失败返回 None。"""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            self._log("warn", "Pillow 未安装，无法渲染报告图片")
            return None

        import math

        # ── 主题配色表 ──
        THEMES = {
            "blue": {
                "bg":[(210,228,255),(228,238,255),(243,247,255)],
                "shadow":(218,228,248),"card":(255,255,255),
                "header":(88,115,185),"accent":(115,148,220),"accent2":(175,200,245),
                "pale":(222,232,252),"text":(55,78,135),"sub":(138,158,200),
                "dot":(198,215,250),
                "phase":{"light":(185,210,255),"deep":(95,128,218),"normal":(208,220,240)},
                "grade":{"优秀":(88,128,228),"良好":(120,158,238),"一般":(168,195,238),"较差":(205,215,238)},
            },
            "pink": {
                "bg":[(255,225,235),(255,235,242),(255,245,248)],
                "shadow":(248,220,230),"card":(255,255,255),
                "header":(210,115,145),"accent":(238,150,175),"accent2":(252,195,215),
                "pale":(252,228,238),"text":(155,65,95),"sub":(205,140,160),
                "dot":(250,205,222),
                "phase":{"light":(255,195,215),"deep":(218,105,148),"normal":(245,215,225)},
                "grade":{"优秀":(218,108,148),"良好":(232,148,175),"一般":(242,185,205),"较差":(245,210,220)},
            },
            "yellow": {
                "bg":[(255,245,205),(255,250,220),(255,253,238)],
                "shadow":(248,238,198),"card":(255,255,255),
                "header":(195,155,45),"accent":(225,185,75),"accent2":(248,220,130),
                "pale":(252,242,198),"text":(135,100,25),"sub":(195,165,85),
                "dot":(248,225,148),
                "phase":{"light":(252,230,145),"deep":(215,168,45),"normal":(245,235,198)},
                "grade":{"优秀":(215,165,42),"良好":(228,188,88),"一般":(240,215,135),"较差":(245,230,178)},
            },
            "purple": {
                "bg":[(225,210,252),(235,222,252),(245,238,255)],
                "shadow":(222,208,248),"card":(255,255,255),
                "header":(128,88,198),"accent":(162,125,225),"accent2":(200,175,245),
                "pale":(232,220,252),"text":(88,55,148),"sub":(162,138,205),
                "dot":(212,195,248),
                "phase":{"light":(215,195,252),"deep":(132,95,205),"normal":(228,218,245)},
                "grade":{"优秀":(128,88,205),"良好":(162,125,225),"一般":(195,168,238),"较差":(218,208,242)},
            },
        }

        theme_name = self.cfg.get("report_theme", "blue")
        t = THEMES.get(theme_name, THEMES["blue"])
        bg0, bg1, bg2 = t["bg"]

        # 模块开关
        mod_duration = self.cfg.get("report_mod_duration", True)
        mod_phases   = self.cfg.get("report_mod_phases",   True)
        mod_score    = self.cfg.get("report_mod_score",    True)
        mod_dream    = self.cfg.get("report_mod_dream",    True)
        mod_overlap  = self.cfg.get("report_mod_overlap",  True)

        W = 750

        # ── 工具函数 ──
        def grad_bg(w, h):
            im = Image.new("RGB", (w, h)); d = ImageDraw.Draw(im)
            for yi in range(h):
                r = yi/h
                if r < 0.5:
                    f = r*2; c0,c1 = bg0,bg1
                else:
                    f = (r-0.5)*2; c0,c1 = bg1,bg2
                col = tuple(int(c0[i]*(1-f)+c1[i]*f) for i in range(3))
                d.line([(0,yi),(w,yi)], fill=col)
            return im

        def ct(draw, cx, y, text, font, fill):
            bb = draw.textbbox((0,0), text, font=font)
            draw.text((cx-(bb[2]-bb[0])//2, y), text, font=font, fill=fill)

        def dot_pat(draw, w, h, col, sp=44, r=2):
            for x in range(0,w,sp):
                for yi in range(0,h,sp):
                    ox = sp//2 if (yi//sp)%2 else 0
                    draw.ellipse([x+ox-r,yi-r,x+ox+r,yi+r], fill=col)

        def star(draw, cx, cy, r, col, n=5):
            p=[]
            for i in range(n*2):
                a=math.pi/n*i-math.pi/2; ri=r if i%2==0 else r*0.42
                p.append((cx+math.cos(a)*ri, cy+math.sin(a)*ri))
            draw.polygon(p, fill=col)

        def bow(draw, cx, cy, col, s=30):
            draw.polygon([(cx-s,cy-s//2),(cx-s,cy+s//2),(cx-3,cy)], fill=col)
            draw.polygon([(cx+s,cy-s//2),(cx+s,cy+s//2),(cx+3,cy)], fill=col)
            draw.ellipse([cx-6,cy-6,cx+6,cy+6], fill=col)

        def heart(draw, cx, cy, r, col):
            p=[]
            for i in range(360):
                a=math.radians(i)
                x=r*(16*math.sin(a)**3)/16
                y2=-r*(13*math.cos(a)-5*math.cos(2*a)-2*math.cos(3*a)-math.cos(4*a))/16
                p.append((cx+x,cy+y2))
            draw.polygon(p, fill=col)

        def sun(draw, cx, cy, r, col):
            draw.ellipse([cx-r,cy-r,cx+r,cy+r], fill=col)
            for i in range(8):
                a=math.radians(i*45)
                draw.line([(cx+math.cos(a)*(r+3),cy+math.sin(a)*(r+3)),
                           (cx+math.cos(a)*(r+10),cy+math.sin(a)*(r+10))], fill=col, width=2)

        def flower(draw, cx, cy, r, col):
            for i in range(6):
                a=math.radians(i*60)
                px,py=cx+math.cos(a)*r*0.55,cy+math.sin(a)*r*0.55
                draw.ellipse([px-r*0.38,py-r*0.38,px+r*0.38,py+r*0.38], fill=col)
            draw.ellipse([cx-r*0.22,cy-r*0.22,cx+r*0.22,cy+r*0.22], fill=(255,255,255))

        def moon(draw, cx, cy, r, col, bg_col):
            draw.ellipse([cx-r,cy-r,cx+r,cy+r], fill=col)
            draw.ellipse([cx+r//3,cy-r,cx+r//3+int(r*1.6),cy+r], fill=bg_col)

        def dandelion(draw, cx, cy, r, col):
            for i in range(12):
                a=math.radians(i*30)
                x2,y2=cx+math.cos(a)*r,cy+math.sin(a)*r
                draw.line([(cx,cy),(x2,y2)], fill=col, width=1)
                draw.ellipse([x2-3,y2-3,x2+3,y2+3], fill=col)

        def deco_elem(draw, cx, cy, size=10):
            a2, bg_c = t["accent2"], bg0
            if theme_name=="blue":   star(draw,cx,cy,size,a2)
            elif theme_name=="pink": heart(draw,cx,cy,size,a2)
            elif theme_name=="yellow": sun(draw,cx,cy,size,a2)
            elif theme_name=="purple": dandelion(draw,cx,cy,size,a2)

        def header_center(draw, cx, cy):
            a2, hc = t["accent2"], t["header"]
            if theme_name=="blue":    bow(draw,cx,cy,(255,255,255),13)
            elif theme_name=="pink":  heart(draw,cx,cy,10,(255,255,255))
            elif theme_name=="yellow": sun(draw,cx,cy,10,(255,255,255))
            elif theme_name=="purple": moon(draw,cx,cy,13,(255,255,255),hc)

        # 加载字体
        try:
            f_title = ImageFont.truetype(FONT_BOLD, 44)
            f_date  = ImageFont.truetype(FONT_REG,  24)
            f_label = ImageFont.truetype(FONT_REG,  21)
            f_big   = ImageFont.truetype(FONT_BOLD, 56)
            f_mid   = ImageFont.truetype(FONT_BOLD, 30)
            f_small = ImageFont.truetype(FONT_REG,  19)
            f_score = ImageFont.truetype(FONT_BOLD, 58)
            f_tiny  = ImageFont.truetype(FONT_REG,  16)
        except Exception:
            self._log("warn", "字体文件加载失败，跳过图片渲染")
            return None

        # 数据准备
        bot_name = self.cfg.get("admin_name", "陆渊")
        date_str = log.get("date","")
        try:
            date_display = datetime.strptime(date_str,"%Y-%m-%d").strftime("%Y年%m月%d日")
        except Exception:
            date_display = date_str
        total_sec = log.get("duration_seconds") or 1
        total_min = total_sec / 60
        dur_h = total_sec / 3600

        H_CANVAS = 1200
        img = grad_bg(W, H_CANVAS)
        draw = ImageDraw.Draw(img)

        # 背景散点
        dot_pat(draw, W, H_CANVAS, t["dot"])

        # 背景装饰
        card_top = 155
        a2 = t["accent2"]
        scattered = [(60,78),(W-60,78),(38,310),(W-38,355),(52,680),(W-52,630),(48,980),(W-48,930)]
        for sx,sy in scattered:
            deco_elem(draw, sx, sy, 9)

        # 标题
        ct(draw, W//2, 52, f"{bot_name} 的晚安报告", f_title, t["text"])
        ct(draw, W//2, 108, date_display, f_date, t["sub"])

        # 标题两侧装饰
        if theme_name=="blue":
            bow(draw,78,76,a2,26); bow(draw,W-78,76,a2,26)
        elif theme_name=="pink":
            heart(draw,72,76,16,a2); heart(draw,W-72,76,16,a2)
        elif theme_name=="yellow":
            sun(draw,72,76,13,a2); sun(draw,W-72,76,13,a2)
        elif theme_name=="purple":
            moon(draw,72,76,20,a2,bg0); moon(draw,W-88,76,16,a2,bg0)

        # 卡片阴影+主卡
        draw.rounded_rectangle([46,card_top+5,W-40,H_CANVAS-32], radius=36, fill=t["shadow"])
        draw.rounded_rectangle([40,card_top,W-44,H_CANVAS-38], radius=36, fill=t["card"])

        # 顶部色条
        bl = Image.new("RGBA",(W,H_CANVAS),(0,0,0,0))
        bd = ImageDraw.Draw(bl)
        bd.rounded_rectangle([40,card_top,W-44,card_top+54], radius=36, fill=t["header"]+(255,))
        bd.rectangle([40,card_top+28,W-44,card_top+54], fill=t["header"]+(255,))
        img = img.convert("RGBA"); img.alpha_composite(bl); img = img.convert("RGB")
        draw = ImageDraw.Draw(img)

        header_center(draw, W//2, card_top+27)
        for sx in [150,W-150,240,W-240,340,W-340]:
            star(draw,sx,card_top+27,4,(255,255,255))

        # 入睡/起床
        y = card_top + 76
        ct(draw,W*3//10,y,"入睡",f_label,t["sub"])
        ct(draw,W*7//10,y,"起床",f_label,t["sub"])
        y += 32
        ct(draw,W*3//10,y,log.get("sleep_start_iso","?"),f_mid,t["text"])
        ct(draw,W*7//10,y,log.get("wake_time_iso","?"),f_mid,t["text"])
        ct(draw,W//2,y+2,"→",f_mid,t["accent2"])
        y += 52
        draw.line([(80,y),(W-80,y)],fill=t["pale"],width=1)

        # 时长
        if mod_duration:
            y += 22
            ct(draw,W//2,y,f"共睡 {dur_h:.1f} 小时",f_big,t["text"])
            y += 78

        # 周期轨迹
        if mod_phases:
            ct(draw,W//2,y,"睡眠周期轨迹",f_label,t["sub"])
            y += 34
            bx0,bx1,bh = 70,W-70,38; bw=bx1-bx0
            draw.rounded_rectangle([bx0,y,bx1,y+bh],radius=bh//2,fill=t["pale"])
            phases = log.get("phases",[])
            wake_ts = log.get("wake_time") or (log.get("sleep_start",0)+total_sec)
            bar_l=Image.new("RGBA",(W,H_CANVAS),(0,0,0,0))
            bl2=ImageDraw.Draw(bar_l)
            xc=float(bx0)
            for i,seg in enumerate(phases):
                st=seg.get("ts",0)
                et=phases[i+1]["ts"] if i+1<len(phases) else wake_ts
                sw=max(1,(et-st)/60/total_min*bw)
                col=t["phase"].get(seg.get("phase","normal"),(210,220,240))+(255,)
                bl2.rectangle([xc,y,xc+sw,y+bh],fill=col); xc+=sw
            bm=Image.new("L",(W,H_CANVAS),0)
            ImageDraw.Draw(bm).rounded_rectangle([bx0,y,bx1,y+bh],radius=bh//2,fill=255)
            bar_l.putalpha(bm)
            img=img.convert("RGBA"); img.alpha_composite(bar_l); img=img.convert("RGB")
            draw=ImageDraw.Draw(img)
            y+=bh+16
            lx=W//2-158
            for lb,col in [("浅睡",t["phase"]["light"]),("深睡",t["phase"]["deep"]),("正常",t["phase"]["normal"])]:
                draw.ellipse([lx,y+2,lx+15,y+17],fill=col)
                draw.text((lx+20,y),lb,font=f_tiny,fill=t["sub"]); lx+=108
            y+=44

        draw.line([(80,y),(W-80,y)],fill=t["pale"],width=1); y+=28

        # 评分
        if mod_score:
            score=log.get("score")
            if score:
                gc=t["grade"].get(score.get("grade",""),t["accent"])
                cr=85; cx=W//2
                for i in range(16):
                    a0=math.radians(i*22.5-4); a1=math.radians(i*22.5+13)
                    draw.arc([cx-cr-8,y-8,cx+cr+8,y+cr*2+8],start=math.degrees(a0),end=math.degrees(a1),fill=t["pale"],width=3)
                draw.ellipse([cx-cr,y,cx+cr,y+cr*2],fill=(255,255,255),outline=gc,width=6)
                bb=draw.textbbox((0,0),str(score.get("total","?")),font=f_score)
                draw.text((cx-(bb[2]-bb[0])//2,y+cr-38),str(score.get("total","?")),font=f_score,fill=gc)
                ct(draw,cx,y+cr+30,score.get("grade",""),f_label,gc)
                deco_elem(draw,cx-cr-28,y+cr,14)
                deco_elem(draw,cx+cr+28,y+cr,14)
                y+=cr*2+26
                ct(draw,W//2,y,f"深睡占比 {score.get('deep_ratio_pct','?')}%　被吵醒 {score.get('woken_count',0)} 次",f_small,t["sub"])
                y+=40
                draw.line([(80,y),(W-80,y)],fill=t["pale"],width=1); y+=24

        # 梦境
        if mod_dream:
            dream=log.get("dream")
            if dream and dream.get("clarity")!="forgotten":
                deco_elem(draw,90,y+13,8); deco_elem(draw,W-90,y+13,8)
                ct(draw,W//2,y,f"做了个{dream.get('type','')}的梦",f_label,t["text"])
                y+=40

        # 重叠
        if mod_overlap and overlap_hint:
            y+=8
            draw.rounded_rectangle([70,y,W-70,y+56],radius=20,fill=t["pale"])
            ct(draw,W//2,y+16,overlap_hint,f_small,t["text"])
            y+=62

        # 落款
        y+=14; deco_elem(draw,W//2,y+18,18)
        ct(draw,W//2,y+46,"晚安 ✦",f_small,t["sub"]); y+=72

        img=img.crop((0,0,W,max(y+20,860)))
        out_path=str(DATA_DIR/f"report_{log.get('date','today')}.png")
        img.save(out_path,"PNG")
        return out_path

    def _calc_overlap_hours(self) -> float | None:
        """
        计算今天"两人都清醒"的重叠小时数（用管理员时区偏移推算对方作息）。
        只在开启了时区功能时才有意义。返回 None 表示无法计算。
        """
        if not self.cfg.get("timezone_enabled", False):
            return None
        offset_h = float(self.cfg.get("timezone_offset_hours", 0))
        if offset_h == 0:
            return None
        now_local = self._local_now()
        # 陆渊的起床/睡觉时间（服务器时间）
        bot_wake = self._parse_time(self._get_day_wake_time(now_local))
        bot_sleep = self._parse_time(self._get_day_sleep_time(now_local))
        bot_wake_min  = bot_wake.hour * 60 + bot_wake.minute
        bot_sleep_min = bot_sleep.hour * 60 + bot_sleep.minute
        # 管理员时区下对应的时间（反向偏移换算到Bot视角）
        admin_wake_cfg  = self.cfg.get("admin_wake_time", "07:00")
        admin_sleep_cfg = self.cfg.get("admin_sleep_time", "23:00")
        admin_wake_t  = self._parse_time(admin_wake_cfg)
        admin_sleep_t = self._parse_time(admin_sleep_cfg)
        # 管理员作息换算到Bot所在时区的分钟数
        admin_wake_min  = (admin_wake_t.hour * 60 + admin_wake_t.minute - int(offset_h * 60)) % (24*60)
        admin_sleep_min = (admin_sleep_t.hour * 60 + admin_sleep_t.minute - int(offset_h * 60)) % (24*60)
        # 计算两段"清醒时间窗口"的重叠（简化为单日计算）
        def overlap_1d(a0, a1, b0, b1):
            return max(0, min(a1, b1) - max(a0, b0))
        ov = overlap_1d(bot_wake_min, bot_sleep_min, admin_wake_min, admin_sleep_min)
        return round(ov / 60, 1) if ov > 0 else 0.0

    def _local_now(self) -> datetime:
        """
        业务时间判断统一用这个，而不是裸的 datetime.now()。
        开启时区偏移后（异地场景，比如管理员在国外），睡觉/起床/午休等所有时间判断
        都按这个偏移后的时间算，不影响日志和数据文件名（那些仍用服务器本地时间）。
        """
        now = datetime.now()
        if self.cfg.get("timezone_enabled", False):
            offset_h = float(self.cfg.get("timezone_offset_hours", 0))
            now = now + timedelta(hours=offset_h)
        return now

    def _local_dt(self, ts: float) -> datetime:
        """把一个具体的时间戳（如入睡/起床的 time.time()）按时区偏移转成当地时间，用于展示。"""
        dt = datetime.fromtimestamp(ts)
        if self.cfg.get("timezone_enabled", False):
            offset_h = float(self.cfg.get("timezone_offset_hours", 0))
            dt = dt + timedelta(hours=offset_h)
        return dt

    def _parse_time(self, s: str):
        from datetime import time as dtime
        try:
            h, m = map(int, str(s).split(":"))
            return dtime(h, m)
        except Exception:
            return dtime(23, 0)

    def _in_range(self, cur, start, end) -> bool:
        return (start <= cur <= end) if start <= end else (cur >= start or cur <= end)

    def _get_day_sleep_time(self, now: datetime) -> str:
        if not self.cfg.get("schedule_enabled", True):
            return self.cfg.get("sleep_time", "23:00")
        key = "schedule_" + ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"][now.weekday()]
        val = str(self.cfg.get(key, "23:00,08:00"))
        return val.split(",")[0].strip() if "," in val else self.cfg.get("sleep_time", "23:00")

    def _get_day_wake_time(self, now: datetime) -> str:
        if not self.cfg.get("schedule_enabled", True):
            return self.cfg.get("wake_time", "08:00")
        key   = "schedule_" + ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"][now.weekday()]
        parts = str(self.cfg.get(key, "23:00,08:00")).split(",")
        wake_str = parts[1].strip() if len(parts) > 1 else self.cfg.get("wake_time", "08:00")
        sleep_str = parts[0].strip() if len(parts) > 0 else self.cfg.get("sleep_time", "23:00")
        # 保护：起床时间如果和睡觉时间间隔太短（小于1小时，按跨天计算），
        # 说明这天的配置很可能填错了，回退到全局默认起床时间，避免刚睡下就立刻被判定该起床
        try:
            st = self._parse_time(sleep_str)
            wt = self._parse_time(wake_str)
            sleep_min = st.hour * 60 + st.minute
            wake_min = wt.hour * 60 + wt.minute
            gap = (wake_min - sleep_min) % (24 * 60)
            if gap < 60:
                self._log("warn", f"{key} 配置的起床时间({wake_str})与睡觉时间({sleep_str})间隔过短，回退到默认起床时间")
                return self.cfg.get("wake_time", "08:00")
        except Exception:
            pass
        return wake_str

    def _inject_prompt(self, event: AstrMessageEvent, prompt: str):
        try:
            existing = event.get_extra("system_prompt") or ""
            event.set_extra("system_prompt", (existing + "\n\n" + prompt).strip())
        except Exception:
            pass

    async def _generate_proactive_message(self, instruction: str, umo: str | None = None) -> str:
        """
        生成一条"主动消息"（早安、晚安、被吵醒后汇报等场景用）。
        instruction 是给LLM的系统指令（比如"你刚起床了，给管理员发条早安"），
        LLM会结合人设和上下文生成一句自然的话，而不是把instruction原文当成台词发出去。
        生成失败时返回空字符串，调用方应该跳过发送，而不是回退发原始提示词
        （发提示词原文给用户看，体验上比不发更糟）。
        """
        try:
            provider = self.context.get_using_provider(umo=umo) if umo else self.context.get_using_provider()
            if not provider:
                return ""
            system_prompt = (
                "你正在扮演一个角色，现在需要根据下面的场景说明，主动说一句话。"
                "直接给出角色会说的话本身，不要加任何解释、不要带引号、不要出现旁白或括号说明，"
                "语气要自然、简短，像真的在和对方聊天一样。\n\n"
                f"场景说明：{instruction}"
            )
            resp = await provider.text_chat(prompt=system_prompt, session_id="sleep_sense_proactive_internal")
            if resp and resp.completion_text:
                return resp.completion_text.strip()
        except Exception as e:
            self._log("warn", f"主动消息生成失败: {e}")
        return ""

    async def _send_to_admin(self, content: str):
        admin_qq = self.cfg.get("admin_qq", "")
        if not admin_qq:
            return
        try:
            await self.context.send_message(
                f"aiocqhttp:FriendMessage:{admin_qq}",
                [{"type": "text", "data": {"text": content}}]
            )
        except Exception as e:
            self._log("warn", f"发送给管理员失败: {e}")

    async def _report_to_admin_later(self):
        await asyncio.sleep(30)
        if self.cfg.get("wake_report_context_aware", True):
            keywords_raw = self.cfg.get("wake_report_goodnight_keywords", "晚安,睡了,去睡了,洗澡睡觉,睡觉去了")
            keywords = [w.strip() for w in str(keywords_raw).split(",") if w.strip()]
            if any(kw in (self._last_admin_text or "") for kw in keywords):
                self._log("info", "跳过被吵醒汇报：已和管理员互道晚安")
                return
        p = "你根据现在对话历史，考虑要不要跟管理员说自己被吵醒了。可以问一下管理员睡了吗？结合吵醒原因自行决定。"
        msg = await self._generate_proactive_message(p)
        if msg:
            await self._send_to_admin(msg)

    # ── 持久化（state/alarms/dreams 用自己的 json，配置用 AstrBotConfig） ──
    def _load_state(self) -> dict:
        default = {
            "sleep_state": S.AWAKE, "sleep_start": None,
            "last_sleep_duration": 28800, "consecutive_overtime": 0,
            "weekly_overtime": 0, "nightmare_tonight": False,
            "sleep_cycle_phase": "normal", "custom_wake_time": None,
            "overtime_decided_today": False, "weekend_asked_today": False,
            "woken_by_others_today": [], "resleep_pending": False,
            "resleep_grace_started": False, "presleep_pending": False,
            "goodnight_said_today": False, "special_wake_done_today": False,
            "nap_start": None, "nap_is_critical": False, "nap_end_offset_min": 0,
            "overtime_reason": None, "nap_cooldown_until": 0, "resleep_cooldown_until": 0,
            "last_report_push_ts": 0, "report_pushed_date": None,
            "last_wake_ts": None,
            "overtime_since": None,
        }
        loaded = self._load_json(STATE_PATH, {})
        return {**default, **loaded}   # 旧文件缺字段时自动补默认值

    def _save_state(self):
        self._save_json(STATE_PATH, self.state)
        self._publish_bus_state()

    def _publish_bus_state(self):
        """把当前睡眠状态写进跨插件联动总线（纯文件协议，详见 astrbot_plugin_air_sense
        的 README），给读空气/主动分享之类的插件用，让它们知道"现在不方便主动说话"。
        这里只是把状态写出去，不强制任何插件来读，没人读也完全不影响本插件本身。"""
        try:
            cur = self.state.get("sleep_state", S.AWAKE)
            label_map = {
                S.AWAKE: "清醒",
                S.LAZY: "慵懒中",
                S.SLEEPING: "睡觉中",
                S.NAPPING: "午休中",
                S.OVERTIME: "熬夜中",
            }
            dnd = cur in (S.SLEEPING, S.NAPPING)
            data = {
                "plugin": "sleep_sense",
                "label": label_map.get(cur, str(cur)),
                "do_not_disturb": dnd,
                "updated_at": time.time(),
                # 安全网：万一进程异常退出忘了把状态切回清醒，免打扰也不会永久卡死，
                # 12小时后自动失效。正常情况下醒来时这里会被下一次 _save_state 覆盖掉。
                "expires_at": time.time() + 12 * 3600,
            }
            path = _bus_dir() / "sleep_sense.json"
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            tmp.replace(path)
        except Exception as e:
            logger.debug(f"[sleep_sense] 发布联动总线状态失败: {e}")

    # ── 每日睡眠日志（入睡/阶段切换轨迹/起床/评分，供 WebUI 和指令查看） ──
    def _open_daily_log(self, start_ts: float):
        """
        入睡瞬间开启今晚的一条日志。
        如果是二次入睡（前一条记录还没真正结束，wake_time是None），延续那一条记录，
        只追加一个"重新入睡"的阶段标记，不要新开一条——否则整晚会被拆成两条独立记录，
        "昨晚睡眠时间线"只能看到最后一段，前面睡的时长和轨迹全部丢失。
        """
        if self.daily_log and self.daily_log[-1].get("wake_time") is None:
            local_resume = self._local_dt(start_ts)
            self.daily_log[-1]["phases"].append({
                "phase": "light", "ts": start_ts, "time": local_resume.strftime("%H:%M"),
                "resleep": True,  # 标记这是二次入睡续上的点，前端可以特殊展示
            })
            self._save_json(DAILY_LOG_PATH, self.daily_log)
            return

        local_start = self._local_dt(start_ts)
        entry = {
            "date": local_start.strftime("%Y-%m-%d"),
            "sleep_start": start_ts,
            "sleep_start_iso": local_start.strftime("%H:%M"),
            "phases": [{"phase": "light", "ts": start_ts, "time": local_start.strftime("%H:%M")}],
            "wake_time": None,
            "wake_time_iso": None,
            "duration_seconds": None,
            "woken_count": 0,
            "dream": None,
            "score": None,
            # 如果今天经历过熬夜才入睡，把熬夜原因也存下来，事后在"睡眠时间线"里也能看到，
            # 不依赖实时state（一旦切换状态，state里的overtime_reason就查不到了）
            "overtime_reason": self.state.get("overtime_reason"),
        }
        self.daily_log.append(entry)
        max_days = int(self.cfg.get("daily_log_max_days", 30))
        if len(self.daily_log) > max_days:
            self.daily_log = self.daily_log[-max_days:]
        self._save_json(DAILY_LOG_PATH, self.daily_log)

    def _mark_cycle_phase(self, phase: str):
        """睡眠阶段切换瞬间（浅睡↔深睡↔正常）记录时间点，用于回看“几点进入深睡”。"""
        if not self.daily_log:
            return
        entry = self.daily_log[-1]
        if entry.get("wake_time") is not None:
            return  # 已经收尾的旧记录不再追加
        now_ts = time.time()
        entry["phases"].append({"phase": phase, "ts": now_ts, "time": self._local_dt(now_ts).strftime("%H:%M")})
        self._save_json(DAILY_LOG_PATH, self.daily_log)

    def _note_woken(self, uid: str = "", is_admin: bool = False, is_group: bool = False,
                     trigger_count: int = 0, needed_base: int = 0, multiplier: float = 1.0):
        """被吵醒一次，记进当晚日志（含原因、当时睡眠阶段、触发次数、倍数），影响后面的睡眠评分（连续性维度）。"""
        if not self.daily_log:
            return
        entry = self.daily_log[-1]
        if entry.get("wake_time") is not None:
            return
        entry["woken_count"] = entry.get("woken_count", 0) + 1
        who = "管理员" if is_admin else "群里的人" if is_group else "对方"
        scene = "群聊艾特" if is_group else "私聊"
        phase_cn = {"light": "浅睡期", "deep": "深睡期", "normal": "正常睡眠期"}
        phase = self.state.get("sleep_cycle_phase", "normal")
        reasons = entry.get("woken_reasons", [])
        reasons.append({
            "ts": time.time(),
            "time": self._local_dt(time.time()).strftime("%H:%M"),
            "who": who, "scene": scene, "is_admin": is_admin,
            "phase": phase, "phase_cn": phase_cn.get(phase, phase),
            "trigger_count": trigger_count,
            "needed_base": needed_base,
            "multiplier": multiplier,
        })
        entry["woken_reasons"] = reasons
        self._save_json(DAILY_LOG_PATH, self.daily_log)

    def _note_woken_by_others(self, is_group: bool):
        """被“别人”（非管理员）吵醒时记录一下，供今天的熬夜决策参考。"""
        scene = "群聊" if is_group else "私聊"
        woken_today = self.state.get("woken_by_others_today", [])
        woken_today.append(scene)
        self.state["woken_by_others_today"] = woken_today
        self._save_state()

    def _close_daily_log(self):
        """起床瞬间收尾今晚的日志：写入起床时间、总时长、关联梦境、计算睡眠评分。"""
        if not self.daily_log:
            return
        entry = self.daily_log[-1]
        if entry.get("wake_time") is not None:
            return
        now_ts = time.time()
        entry["wake_time"] = now_ts
        entry["wake_time_iso"] = self._local_dt(now_ts).strftime("%H:%M")

        # 时长不能简单用"起床时刻 - 最初入睡时刻"，因为如果中途被吵醒过、又二次入睡，
        # 中间那段清醒的时间不算睡眠，必须从总跨度里减掉。
        # woken_reasons里每条记录的ts是"被吵醒的时刻"，phases里标了resleep=True的点是
        # "二次入睡续上的时刻"，按时间顺序配对，把每一段"吵醒→续睡"之间的间隙累加起来扣除。
        sleep_start_val = entry.get("sleep_start")
        start = sleep_start_val if sleep_start_val is not None else now_ts
        total_span = now_ts - start
        awake_gap = 0.0
        woken_ts_list = sorted(r.get("ts", 0) for r in entry.get("woken_reasons", []))
        resleep_ts_list = sorted(p.get("ts", 0) for p in entry.get("phases", []) if p.get("resleep"))
        for i, woken_ts in enumerate(woken_ts_list):
            # 找这次被吵醒之后最近的一次续睡时刻
            resume_ts = next((t for t in resleep_ts_list if t > woken_ts), None)
            if resume_ts:
                awake_gap += max(0, resume_ts - woken_ts)
            else:
                # 被吵醒了但还没找到对应的续睡点，说明这段一直清醒到现在（起床那一刻）
                awake_gap += max(0, now_ts - woken_ts)
        entry["duration_seconds"] = round(max(0, total_span - awake_gap), 1)

        if self.dreams:
            last_dream = self.dreams[-1]
            if last_dream.get("sleep_start") == entry.get("sleep_start"):
                entry["dream"] = {
                    "type": last_dream.get("type"),
                    "clarity": last_dream.get("clarity"),
                    "recalled": last_dream.get("recalled"),
                    "content": last_dream.get("content"),
                }

        if self.cfg.get("sleep_score_enabled", True):
            entry["score"] = self._calc_sleep_score(entry)

        self._save_json(DAILY_LOG_PATH, self.daily_log)

    def _calc_sleep_score(self, entry: dict) -> dict:
        """
        参考手环睡眠报告思路打分（满分100）：
        - 时长 40 分：与目标时长的接近程度，过短扣分多，过长略扣
        - 深睡占比 25 分：深睡时间占总时长的比例，20%~25% 左右最佳
        - 连续性 20 分：被吵醒次数越多扣分越多
        - 入睡及时性 15 分：实际入睡时间和配置睡觉时间的偏差
        """
        duration_sec = entry.get("duration_seconds") or 0
        duration_h = duration_sec / 3600

        # 1. 时长分
        target_h = float(self.cfg.get("sleep_score_target_hours", 8))
        diff_ratio = abs(duration_h - target_h) / max(target_h, 1)
        duration_score = max(0, 40 * (1 - diff_ratio * 1.4))

        # 2. 深睡占比分
        phases = entry.get("phases", [])
        deep_sec = 0.0
        for i, p in enumerate(phases):
            if p.get("phase") != "deep":
                continue
            seg_start = p.get("ts")
            seg_end = phases[i + 1]["ts"] if i + 1 < len(phases) else entry.get("wake_time", seg_start)
            deep_sec += max(0, seg_end - seg_start)
        deep_ratio = (deep_sec / duration_sec) if duration_sec > 0 else 0
        # 20~25% 视为理想区间，偏离越多扣分越多
        ideal_low, ideal_high = 0.18, 0.27
        if ideal_low <= deep_ratio <= ideal_high:
            deep_score = 25
        else:
            dist = (ideal_low - deep_ratio) if deep_ratio < ideal_low else (deep_ratio - ideal_high)
            deep_score = max(0, 25 - dist * 100)

        # 3. 连续性分（被吵醒次数）
        woken = entry.get("woken_count", 0)
        continuity_score = max(0, 20 - woken * 6)

        # 4. 入睡及时性分
        try:
            target_sleep_time = self._parse_time(self.cfg.get("sleep_time", "23:00"))
            sleep_dt = self._local_dt(entry.get("sleep_start", time.time()))
            target_dt = sleep_dt.replace(hour=target_sleep_time.hour, minute=target_sleep_time.minute, second=0)
            delay_min = (sleep_dt - target_dt).total_seconds() / 60
            if delay_min < 0:
                delay_min = 0  # 提前睡不扣分
            timeliness_score = max(0, 15 - delay_min / 10)
        except Exception:
            timeliness_score = 15

        total = round(duration_score + deep_score + continuity_score + timeliness_score)
        total = max(0, min(100, total))

        if total >= 90: grade = "优秀"
        elif total >= 75: grade = "良好"
        elif total >= 60: grade = "一般"
        else: grade = "较差"

        return {
            "total": total,
            "grade": grade,
            "duration_score": round(duration_score, 1),
            "deep_score": round(deep_score, 1),
            "continuity_score": round(continuity_score, 1),
            "timeliness_score": round(timeliness_score, 1),
            "deep_ratio_pct": round(deep_ratio * 100, 1),
            "woken_count": woken,
        }

    def _load_json(self, path: Path, default):
        try:
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return default

    def _save_json(self, path: Path, data):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(path)   # 原子写入，避免文件损坏
        except Exception as e:
            logger.error(f"[sleep_sense] save_json {path}: {e}")

    # ── 日志 ──────────────────────────────────────────────────────────
    def _log(self, level: str, msg: str):
        levels = {"debug": 0, "info": 1, "warn": 2}
        if levels.get(level, 1) < levels.get(self._log_level, 1):
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{ts}][{level.upper()}] {msg}\n")
        except Exception:
            pass
        (logger.warning if level == "warn" else logger.info)(f"[sleep_sense] {msg}")

    def _record_stat(self, key: str, val):
        p = STATS_DIR / f"{datetime.now().strftime('%Y-W%W')}.json"
        data = self._load_json(p, {"records": []})
        data["records"].append({"key": key, "val": val, "ts": time.time()})
        self._save_json(p, data)

    async def terminate(self):
        self._save_state()
        self._save_json(ALARMS_PATH, self.alarms)
        self._save_json(DREAMS_PATH, self.dreams)
        self._save_json(DAILY_LOG_PATH, self.daily_log)
        logger.info("[sleep_sense] 插件已卸载，状态已保存")
