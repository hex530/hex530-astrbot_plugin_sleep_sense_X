"""
astrbot_plugin_sleep_sense v1.2.0
让 Bot 拥有真实睡眠感知：睡觉、慵懒、熬夜、补觉、吵醒、睡眠周期、做梦
作者: 夕小柠
"""

import asyncio
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# ── 数据目录 ──────────────────────────────────────────────────────────────
DATA_DIR = Path("data/sleep_sense")
STATE_PATH  = DATA_DIR / "state.json"
ALARMS_PATH = DATA_DIR / "alarms.json"
DREAMS_PATH = DATA_DIR / "dreams.json"
LOG_PATH    = DATA_DIR / "logs" / "sleep.log"
STATS_DIR   = DATA_DIR / "stats"


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
    try:
        if hasattr(event, "is_at_me") and callable(event.is_at_me):
            return event.is_at_me()
    except Exception:
        pass
    try:
        raw = getattr(event.message_obj, "raw_message", "") or ""
        return "[CQ:at" in str(raw)
    except Exception:
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
        self.alarms: list = self._load_json(ALARMS_PATH, [])
        self.dreams: list = self._load_json(DREAMS_PATH, [])

        self._msg_counters: dict[str, int]   = {}
        self._woken_flag:   dict[str, bool]  = {}
        self._last_admin_ts: float           = time.time()
        self._last_others_ts: dict[str, float] = {}
        self._pre_sleep_ctx: list[str]       = []
        self._dream_generated_tonight: bool  = False
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

    # ═══════════════════════════════════════════════════════════════════
    # 消息主入口
    # ═══════════════════════════════════════════════════════════════════
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        async with self._lock:
            uid      = str(event.get_sender_id())
            is_admin = self._is_admin(uid)
            is_group = _is_group(event)
            is_at    = _is_at_me(event) if is_group else True
            text     = event.message_str or ""

            if is_admin:
                self._last_admin_ts = time.time()
            else:
                self._last_others_ts[uid] = time.time()

            cur = self.state["sleep_state"]
            if cur in (S.AWAKE, S.LAZY, S.OVERTIME) and text:
                self._collect_dream_ctx(text)

            if cur == S.SLEEPING:
                ok = await self._handle_sleeping(event, uid, is_admin, is_group, is_at, text)
                if not ok:
                    return
            elif cur == S.NAPPING:
                ok = await self._handle_napping(event, uid, is_admin, is_group, is_at)
                if not ok:
                    return
            else:
                inject = self._build_inject(is_admin)
                if inject:
                    self._inject_prompt(event, inject)

    # ═══════════════════════════════════════════════════════════════════
    # 睡觉中处理
    # ═══════════════════════════════════════════════════════════════════
    async def _handle_sleeping(self, event, uid, is_admin, is_group, is_at, text) -> bool:
        if not self.cfg.get("wake_trigger_enabled", True):
            return False
        if is_group and not is_at:
            return False
        if not is_admin and not is_group and not self.cfg.get("scene_private_others", True):
            return False
        if not is_admin and is_group and not self.cfg.get("scene_group_others", True):
            return False
        if is_admin and not is_group and not self.cfg.get("scene_private_admin", True):
            return False
        if is_admin and is_group and not self.cfg.get("scene_group_admin", True):
            return False

        if is_admin:
            alert_raw = self.cfg.get("wake_alert_words", "醒醒,有大事,紧急,快起来")
            alerts = [w.strip() for w in str(alert_raw).split(",") if w.strip()]
            if not is_group or self.cfg.get("wake_alert_group_enabled", True):
                if any(w in text for w in alerts):
                    await self._do_wake(event, uid, is_admin, is_group, force=True)
                    return True

        multiplier = self._get_cycle_multiplier()
        key = f"{uid}_{is_group}"
        cnt = self._msg_counters.get(key, 0) + 1
        self._msg_counters[key] = cnt

        if is_admin:
            needed = int(self.cfg.get("wake_count_private_admin", 3)) if not is_group else int(self.cfg.get("wake_count_group_admin", 3))
        else:
            needed = int(self.cfg.get("wake_count_private_others", 10)) if not is_group else int(self.cfg.get("wake_count_group_others", 6))

        if cnt >= max(1, round(needed * multiplier)):
            self._msg_counters[key] = 0
            await self._do_wake(event, uid, is_admin, is_group)
            return True
        return False

    async def _do_wake(self, event, uid, is_admin, is_group, force=False):
        woken_key = f"woken_{uid}_{is_group}"
        already   = self._woken_flag.get(woken_key, False)
        prefix    = self._get_cycle_prompt_prefix()

        if not already:
            if is_admin and not is_group:
                p = self.cfg.get("prompts_wake_private_admin_1", "这是你的管理员，你刚才被吵醒了，很懵很困，可以单发一个「？」或者「嗯？怎么了」。")
            elif is_admin and is_group:
                p = self.cfg.get("prompts_wake_group_admin_1", "管理员在群里艾特你了，把你吵醒了，很懵很困，可以短回复一下。")
            elif not is_group:
                p = self.cfg.get("prompts_wake_private_others_1", "你被他吵醒了，很懵，可以发个问号或者「嗯？」之类的短回复。")
            else:
                p = self.cfg.get("prompts_wake_group_others_1", "你被群里的人艾特吵醒了，很懵，可以简短问一下怎么了。")
            self._woken_flag[woken_key] = True
            self._set_state(S.AWAKE)
            self._emit_state("awake")
            self._log("info", f"被唤醒 uid={uid} group={is_group}")
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

        self._inject_prompt(event, (prefix + p).strip())
        if not is_admin and self.cfg.get("wake_report_to_admin", True):
            asyncio.create_task(self._report_to_admin_later())

    # ═══════════════════════════════════════════════════════════════════
    # 午休/补觉处理
    # ═══════════════════════════════════════════════════════════════════
    async def _handle_napping(self, event, uid, is_admin, is_group, is_at) -> bool:
        if is_group and not is_at:
            return False
        key = f"nap_{uid}_{is_group}"
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
        return False

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
        if cur == S.OVERTIME:
            scope = self.cfg.get("overtime_night_scope", "admin_only")
            if scope == "global" or is_admin:
                p = self.cfg.get("overtime_night_mood_prompt", "")
                if p: parts.append(p)
        return " ".join(p for p in parts if p)

    # ═══════════════════════════════════════════════════════════════════
    # 指令：/睡眠
    # ═══════════════════════════════════════════════════════════════════
    @filter.command("睡眠")
    async def cmd_sleep(self, event: AstrMessageEvent):
        """睡眠插件管理。发送 /睡眠 帮助 查看所有指令。"""
        uid = str(event.get_sender_id())
        is_admin = self._is_admin(uid)
        try:
            is_op = await self.context.is_admin(uid)
        except Exception:
            is_op = False
        if not (is_admin or is_op):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return

        text   = (event.message_str or "").strip()
        action = text.replace("/睡眠", "").strip() or "帮助"
        s      = self.state

        if action == "帮助":
            yield event.plain_result(
                "🌙 sleep_sense 指令帮助\n"
                "━━━━━━━━━━━━━━━\n"
                "📊 查询\n"
                "/睡眠 状态    — 当前状态\n"
                "/睡眠 周期    — 睡眠周期详情\n"
                "/睡眠 统计    — 本周睡眠统计\n"
                "\n🎮 强制切换\n"
                "/睡眠 清醒  /睡眠 慵懒\n"
                "/睡眠 睡觉  /睡眠 熬夜  /睡眠 午休\n"
                "\n🧪 测试\n"
                "/睡眠 测试做梦  — 立刻生成梦境\n"
                "/睡眠 测试起床  — 触发起床流程\n"
                "/睡眠 测试噩梦  — 触发噩梦提示词\n"
                "/睡眠 测试闹钟  — 查看当前闹钟\n"
                "\n⚙️ 其他\n"
                "/睡眠 日志on / 日志off\n"
                "\n💤 梦境\n"
                "/梦境 列表 / 今晚 / 清除"
            )

        elif action == "状态":
            dur = s.get("last_sleep_duration", 0)
            state_cn = {S.AWAKE:"😊 清醒",S.LAZY:"😴 慵懒",S.SLEEPING:"💤 睡眠中",S.NAPPING:"☕ 午休中",S.OVERTIME:"🌃 熬夜"}
            yield event.plain_result(
                f"🌙 状态：{state_cn.get(s.get('sleep_state','?'),'?')}\n"
                f"⏰ 上次睡眠：{dur/3600:.1f}h\n"
                f"🌃 连续熬夜：{s.get('consecutive_overtime',0)}天\n"
                f"📅 本周熬夜：{s.get('weekly_overtime',0)}天"
            )

        elif action == "周期":
            if s.get("sleep_state") != S.SLEEPING:
                yield event.plain_result("当前不在睡眠中，无睡眠周期数据。")
                return
            start   = s.get("sleep_start") or time.time()
            elapsed = (time.time() - start) / 60
            phase   = s.get("sleep_cycle_phase", "normal")
            phase_cn = {"light":"🌙 浅睡期（容易被吵醒）","deep":"😴 深睡期（很难被吵醒）","normal":"💤 正常睡眠期"}
            light = int(self.cfg.get("cycle_light_minutes", 30))
            deep  = int(self.cfg.get("cycle_deep_minutes", 90))
            yield event.plain_result(
                f"💤 当前睡眠周期\n"
                f"━━━━━━━━━━━━━\n"
                f"已睡：{elapsed:.0f} 分钟\n"
                f"当前：{phase_cn.get(phase, phase)}\n"
                f"浅睡期：0~{light} 分钟\n"
                f"深睡期：{light}~{deep} 分钟\n"
                f"吵醒倍数：×{self._get_cycle_multiplier()}"
            )

        elif action == "统计":
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

        elif action == "清醒":
            self._set_state(S.AWAKE); self._emit_state("awake")
            yield event.plain_result("✅ 已强制切换：清醒")
        elif action == "慵懒":
            self._set_state(S.LAZY)
            yield event.plain_result("✅ 已强制切换：慵懒")
        elif action == "睡觉":
            await self._enter_sleep()
            yield event.plain_result("✅ 已强制切换：睡眠")
        elif action == "熬夜":
            self._set_state(S.OVERTIME)
            self.state["consecutive_overtime"] = self.state.get("consecutive_overtime", 0) + 1
            self._save_state()
            yield event.plain_result("✅ 已强制切换：熬夜")
        elif action == "午休":
            await self._enter_nap()
            yield event.plain_result("✅ 已强制切换：午休")

        elif action == "测试做梦":
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

        elif action == "测试起床":
            await self._do_wake_up()
            yield event.plain_result("✅ 已触发起床流程（含梦境浮现）")

        elif action == "测试噩梦":
            self.state["nightmare_tonight"] = False
            p = self.cfg.get("nightmare_prompt", "你刚才做噩梦惊醒了，现在心里有点难受，可以发消息给管理员寻求安慰。")
            await self._send_to_admin(p)
            self._set_state(S.AWAKE); self._emit_state("awake")
            yield event.plain_result("✅ 已触发噩梦惊醒提示词")

        elif action == "测试闹钟":
            if not self.alarms:
                yield event.plain_result("当前没有临时闹钟。")
            else:
                lines = [f"⏰ {a.get('date','')} {a.get('time','')} — {a.get('reason','')}" for a in self.alarms]
                yield event.plain_result("当前闹钟：\n" + "\n".join(lines))

        elif action == "日志on":
            self._log_level = "debug"
            yield event.plain_result("✅ debug 日志已开启")
        elif action == "日志off":
            self._log_level = "info"
            yield event.plain_result("✅ debug 日志已关闭")
        else:
            yield event.plain_result("❓ 未知指令，发送 /睡眠 帮助 查看所有指令")

    # ═══════════════════════════════════════════════════════════════════
    # 指令：/梦境
    # ═══════════════════════════════════════════════════════════════════
    @filter.command("梦境")
    async def cmd_dream(self, event: AstrMessageEvent):
        """查看梦境记录。用法: /梦境 列表|今晚|清除"""
        uid = str(event.get_sender_id())
        is_admin = self._is_admin(uid)
        try:
            is_op = await self.context.is_admin(uid)
        except Exception:
            is_op = False
        if not (is_admin or is_op):
            yield event.plain_result("❌ 权限不足")
            return

        text   = (event.message_str or "").strip()
        action = text.replace("/梦境", "").strip() or "列表"
        clarity_cn = {"clear":"记得清楚","blurry":"有点模糊","feeling_only":"只剩感觉","forgotten":"完全忘了"}

        if action == "列表":
            if not self.dreams:
                yield event.plain_result("📖 暂无梦境记录")
                return
            lines = [f"📅 {d.get('date','')} [{d.get('type','')}] {clarity_cn.get(d.get('clarity',''),'?')}" for d in self.dreams[-5:]]
            yield event.plain_result("最近5条梦境记录：\n" + "\n".join(lines))

        elif action == "今晚":
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

        elif action == "清除":
            self.dreams = []
            self._save_json(DREAMS_PATH, self.dreams)
            self.state.pop("tonight_dream", None)
            self._save_state()
            yield event.plain_result("✅ 梦境记录已清除")
        else:
            yield event.plain_result("可用: 列表 / 今晚 / 清除")

    # ═══════════════════════════════════════════════════════════════════
    # AI 工具：设置临时闹钟
    # ═══════════════════════════════════════════════════════════════════
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
        now = datetime.now()
        cur = self.state["sleep_state"]

        # 慵懒
        if cur == S.AWAKE and self.cfg.get("lazy_enabled", True):
            ls = self._parse_time(self.cfg.get("lazy_start", "22:00"))
            le = self._parse_time(self.cfg.get("lazy_end",   "23:00"))
            if self._in_range(now.time(), ls, le):
                self._set_state(S.LAZY)

        if cur in (S.AWAKE, S.LAZY, S.OVERTIME):
            await self._check_sleep(now)
        if cur == S.SLEEPING:
            await self._check_wake(now)
            self._update_cycle()
            await self._check_nightmare()
        if cur == S.AWAKE:
            await self._check_nap(now)
        if cur in (S.AWAKE, S.LAZY):
            await self._check_overtime_decision(now)

    async def _check_sleep(self, now: datetime):
        if not self.cfg.get("sleep_enabled", True):
            return
        target = self._parse_time(self._get_day_sleep_time(now))
        offset = random.randint(-int(self.cfg.get("sleep_variance", 30)), int(self.cfg.get("sleep_variance", 30)))
        target_dt = now.replace(hour=target.hour, minute=target.minute, second=0) + timedelta(minutes=offset)
        if now < target_dt:
            return
        admin_idle     = (time.time() - self._last_admin_ts) >= int(self.cfg.get("admin_idle_minutes", 30)) * 60
        private_silent = self._is_private_silent()
        if admin_idle and private_silent:
            await self._enter_sleep()
        elif admin_idle and not private_silent:
            asyncio.create_task(self._delayed_sleep(int(self.cfg.get("others_grace_minutes", 3)) * 60))

    async def _check_wake(self, now: datetime):
        wake_str = self.state.get("custom_wake_time") or self._get_day_wake_time(now)
        target   = self._parse_time(wake_str)
        offset   = random.randint(0, int(self.cfg.get("wake_variance", 20)))
        target_dt = now.replace(hour=target.hour, minute=target.minute, second=0) + timedelta(minutes=offset)
        if now >= target_dt:
            await self._do_wake_up()

    async def _check_nap(self, now: datetime):
        if not self.cfg.get("nap_enabled", True):
            return
        last_dur  = self.state.get("last_sleep_duration", 28800)
        critical  = int(self.cfg.get("nap_critical_hours", 2)) * 3600
        min_hours = int(self.cfg.get("nap_min_sleep_hours", 6)) * 3600
        is_crit   = last_dur < critical
        in_window = is_crit
        if not is_crit:
            ws = self._parse_time(self.cfg.get("nap_window_start", "13:00"))
            we = self._parse_time(self.cfg.get("nap_window_end",   "16:00"))
            in_window = self._in_range(now.time(), ws, we)
        if in_window and (self.cfg.get("siesta_enabled", True) or last_dur < min_hours):
            if random.random() < float(self.cfg.get("nap_probability", 0.8)) and self._is_private_silent():
                await self._enter_nap()

    async def _check_overtime_decision(self, now: datetime):
        if not self.cfg.get("overtime_enabled", True) or self.state.get("overtime_decided_today"):
            return
        t  = self._parse_time(self._get_day_sleep_time(now))
        td = now.replace(hour=t.hour, minute=t.minute, second=0)
        if not (td - timedelta(minutes=30) <= now <= td):
            return
        mode   = self.cfg.get("overtime_mode", "probability")
        should = (random.random() < float(self.cfg.get("overtime_probability", 0.1))) if mode == "probability" \
                 else (self.state.get("weekly_overtime", 0) < int(self.cfg.get("overtime_weekly_limit", 2)))
        if should:
            self.state["overtime_decided_today"] = True
            self._save_state()
            await self._send_to_admin(self.cfg.get("overtime_ask_prompt", "今天晚上，你想熬夜吗？由你自己来决定，只回复是或否。"))

    async def _check_nightmare(self):
        if not self.cfg.get("nightmare_enabled", True) or self.state.get("nightmare_tonight"):
            return
        if random.random() < float(self.cfg.get("nightmare_probability", 0.02)) / 60:
            self.state["nightmare_tonight"] = True
            self._save_state()
            await self._send_to_admin(self.cfg.get("nightmare_prompt", "你刚才做噩梦惊醒了，现在心里有点难受，可以发消息给管理员寻求安慰。"))
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
                await self._send_to_admin(f"[闹钟] {a.get('reason','')} 你被唤醒了，自行判断现在的时间，正常回复。")
                self._log("info", f"闹钟触发: {a}")
        self.alarms = [x for x in self.alarms if x not in done]
        if done:
            self._save_json(ALARMS_PATH, self.alarms)

    # ═══════════════════════════════════════════════════════════════════
    # 状态转换
    # ═══════════════════════════════════════════════════════════════════
    async def _enter_sleep(self):
        self._set_state(S.SLEEPING)
        self.state.update({"sleep_start": time.time(), "sleep_cycle_phase": "light",
                           "nightmare_tonight": False, "overtime_decided_today": False})
        self._msg_counters.clear(); self._woken_flag.clear()
        self._dream_generated_tonight = False
        self._save_state()
        self._emit_state("sleep")
        self._log("info", "进入睡眠")
        self._record_stat("sleep_start", datetime.now().isoformat())
        asyncio.create_task(self._dream_scheduler())

    async def _enter_nap(self):
        self._set_state(S.NAPPING)
        self._emit_state("nap")
        self._log("info", "进入午休/补觉")

    async def _do_wake_up(self):
        start = self.state.get("sleep_start")
        if start:
            self.state["last_sleep_duration"] = time.time() - start
        self.state["custom_wake_time"]    = None
        self.state["consecutive_overtime"] = 0
        self._set_state(S.AWAKE)
        self._emit_state("awake")
        self._log("info", "正常起床")
        self._record_stat("wake_time", datetime.now().isoformat())
        await self._dream_recall_on_wake()

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
        if self.state["sleep_state"] not in (S.SLEEPING, S.NAPPING):
            await self._enter_sleep()

    # ═══════════════════════════════════════════════════════════════════
    # 睡眠周期
    # ═══════════════════════════════════════════════════════════════════
    def _update_cycle(self):
        if not self.cfg.get("sleep_cycle_enabled", True):
            return
        elapsed = (time.time() - (self.state.get("sleep_start") or time.time())) / 60
        light = int(self.cfg.get("cycle_light_minutes", 30))
        deep  = int(self.cfg.get("cycle_deep_minutes",  90))
        phase = "light" if elapsed < light else ("deep" if elapsed < deep else "normal")
        self.state["sleep_cycle_phase"] = phase

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
        deep_min = int(self.cfg.get("cycle_deep_minutes", 90))
        trigger  = random.randint(0, int(self.cfg.get("dream_window_minutes", 60))) * 60
        await asyncio.sleep(deep_min * 60 + trigger)
        if self.state["sleep_state"] != S.SLEEPING or self._dream_generated_tonight:
            return
        if random.random() > float(self.cfg.get("dream_probability", 0.7)):
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

        record = {"date": datetime.now().strftime("%Y-%m-%d"), "sleep_start": self.state.get("sleep_start"),
                  "type": dream_type, "content": dream_content, "clarity": clarity,
                  "recalled": will_recall, "ts": time.time()}
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
        await self._send_to_admin(p.replace("{dream}", content).replace("{type}", dtype))
        self._log("info", f"梦境浮现 clarity={clarity}")

    async def _llm_generate(self, prompt: str) -> str:
        try:
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
    def _is_admin(self, uid: str) -> bool:
        return (self.cfg.get("admin_enabled", True)
                and str(uid) == str(self.cfg.get("admin_qq", "")))

    def _is_private_silent(self, thresh_sec: int = 300) -> bool:
        now = time.time()
        return all(now - ts >= thresh_sec for ts in self._last_others_ts.values())

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
        return parts[1].strip() if len(parts) > 1 else self.cfg.get("wake_time", "08:00")

    def _inject_prompt(self, event: AstrMessageEvent, prompt: str):
        try:
            existing = event.get_extra("system_prompt") or ""
            event.set_extra("system_prompt", (existing + "\n\n" + prompt).strip())
        except Exception:
            pass

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
        await self._send_to_admin(
            "你根据现在对话历史，考虑要不要跟管理员说自己被吵醒了。"
            "可以问一下管理员睡了吗？结合吵醒原因自行决定。"
        )

    # ── 持久化（state/alarms/dreams 用自己的 json，配置用 AstrBotConfig） ──
    def _load_state(self) -> dict:
        default = {
            "sleep_state": S.AWAKE, "sleep_start": None,
            "last_sleep_duration": 28800, "consecutive_overtime": 0,
            "weekly_overtime": 0, "nightmare_tonight": False,
            "sleep_cycle_phase": "normal", "custom_wake_time": None,
            "overtime_decided_today": False,
        }
        loaded = self._load_json(STATE_PATH, {})
        return {**default, **loaded}   # 旧文件缺字段时自动补默认值

    def _save_state(self):
        self._save_json(STATE_PATH, self.state)

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
        logger.info("[sleep_sense] 插件已卸载，状态已保存")
