#!/usr/bin/env python3
"""
Claude Code 工具调用能力矩阵测试器。

该脚本面向 qwen2API 的 Anthropic 兼容入口，按模型执行 Claude Code 工具调用场景，
并通过 stream-json 事件、临时文件副作用与最终回答共同判定工具调用能力。
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import http.server
import json
import os
import pathlib
import random
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any, Callable, Iterable

TEXT_MODEL_MODES = {"chat", "thinking", "search", "deep_research", "webdev"}
SKIPPED_MODEL_MODES = {"image", "video", "slides"}
DEFAULT_API_BASE = "https://qwen2api.codeai.de5.net"
DEFAULT_API_KEY_ENVS = ("ANTHROPIC_API_KEY", "QWEN2API_API_KEY")
DEFAULT_ALIASES = (
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-3-haiku",
)

PASS = "PASS"
PARTIAL = "PARTIAL"
FAIL = "FAIL"
SKIP = "SKIP"
UNSTABLE = "UNSTABLE"


@dataclasses.dataclass(frozen=True)
class ModelEntry:
    """表示一个待测模型及其来源。"""

    model_id: str
    mode: str = "alias"
    base_model: str = ""
    source: str = "manual"


@dataclasses.dataclass(frozen=True)
class ToolCall:
    """从 Claude Code stream-json 中提取出的工具调用事件。"""

    name: str
    input: dict[str, Any]
    call_id: str = ""
    event_index: int = -1
    assistant_turn: int = -1


@dataclasses.dataclass
class EventSummary:
    """保存一次 Claude Code 运行的事件解析结果。"""

    tool_calls: list[ToolCall] = dataclasses.field(default_factory=list)
    final_text: str = ""
    assistant_text: str = ""
    result_subtype: str = ""
    raw_event_count: int = 0
    parse_errors: int = 0
    available_tools: list[str] = dataclasses.field(default_factory=list)

    def tool_names(self) -> list[str]:
        """返回本次运行中出现过的工具名。"""
        return [call.name for call in self.tool_calls]

    def has_tool(self, name: str) -> bool:
        """判断是否出现指定工具调用。"""
        return any(call.name == name for call in self.tool_calls)

    def first_tool_input(self, name: str) -> dict[str, Any]:
        """返回指定工具第一次调用的输入参数。"""
        for call in self.tool_calls:
            if call.name == name:
                return call.input
        return {}

    def max_same_turn_count(self, name: str | None = None) -> int:
        """统计单个 assistant turn 内同名或全部工具调用的最大数量。"""
        counts: dict[int, int] = defaultdict(int)
        for call in self.tool_calls:
            if name is None or call.name == name:
                counts[call.assistant_turn] += 1
        return max(counts.values(), default=0)


@dataclasses.dataclass
class CommandResult:
    """保存 Claude Code 子进程执行结果。"""

    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


@dataclasses.dataclass
class ScenarioResult:
    """保存单个模型单个场景的判定结果。"""

    model: str
    scenario: str
    phase: str
    status: str
    reason: str
    duration_seconds: float
    attempts: int
    case_dir: str
    tools_seen: list[str]
    stdout_path: str
    stderr_path: str
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Scenario:
    """定义一个工具调用测试场景。"""

    name: str
    phase: str
    tools: tuple[str, ...]
    seed: Callable[[pathlib.Path], dict[str, Any]]
    prompt: Callable[[pathlib.Path, dict[str, Any]], str]
    validate: Callable[[pathlib.Path, dict[str, Any], EventSummary, CommandResult], tuple[str, str, dict[str, Any]]]
    cleanup: Callable[[dict[str, Any]], None] | None = None
    timeout_seconds: int = 0


class QuietHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """屏蔽本地 WebFetch 测试 HTTP 服务的访问日志。"""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003 - 保持父类签名
        return


class ReusableThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """允许快速回收端口的本地 HTTP 服务。"""

    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    """解析命令行参数并执行测试矩阵。"""
    args = parse_args()
    if not args.execute:
        print("当前为安全预览模式：不会读取 API Key、不会请求线上服务、不会启动 Claude Code。")
        print("如需真实执行，请显式添加 --execute，并通过环境变量提供 ANTHROPIC_API_KEY 或 QWEN2API_API_KEY。")

    api_key = read_api_key(args.api_key_envs) if args.execute else ""
    if args.execute and not api_key:
        env_names = ", ".join(args.api_key_envs)
        print(f"错误：真实执行需要设置 API Key 环境变量：{env_names}", file=sys.stderr)
        return 2

    result_root = pathlib.Path(args.result_dir or make_default_result_dir()).resolve()
    result_root.mkdir(parents=True, exist_ok=True)

    models = resolve_models(args, api_key)
    scenarios = build_scenarios(include_p1=args.include_p1)
    selected_scenarios = filter_scenarios(scenarios, args.tests)
    discovered_tools = discover_available_tools(args, result_root) if args.execute else []
    write_manifest(result_root, args, models, selected_scenarios, discovered_tools)

    if not args.execute:
        print_preview(result_root, models, selected_scenarios, args)
        return 0

    if shutil.which(args.claude_bin) is None:
        print(f"错误：找不到 Claude Code 命令：{args.claude_bin}", file=sys.stderr)
        return 2

    jsonl_path = result_root / "results.jsonl"
    results: list[ScenarioResult] = []
    with jsonl_path.open("a", encoding="utf-8") as fp:
        for model in models:
            if model.mode in SKIPPED_MODEL_MODES:
                result = ScenarioResult(
                    model=model.model_id,
                    scenario="model-mode",
                    phase="skip",
                    status=SKIP,
                    reason=f"模型模式 {model.mode} 不适合 Claude Code 文本工具调用测试",
                    duration_seconds=0.0,
                    attempts=0,
                    case_dir="",
                    tools_seen=[],
                    stdout_path="",
                    stderr_path="",
                    metadata={"mode": model.mode, "source": model.source},
                )
                emit_result(fp, result)
                results.append(result)
                continue

            model_results = run_model_matrix(
                args=args,
                result_root=result_root,
                model=model,
                scenarios=selected_scenarios,
                env_api_key=api_key,
                fp=fp,
                discovered_tools=discovered_tools,
            )
            results.extend(model_results)

    summary_path = write_summary(result_root, results)
    print(f"结果目录：{result_root}")
    print(f"JSONL：{jsonl_path}")
    print(f"汇总：{summary_path}")
    return 0 if all(item.status in {PASS, SKIP} for item in results) else 1


def parse_args() -> argparse.Namespace:
    """构建并解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="运行 qwen2API + Claude Code 工具调用能力矩阵测试。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--execute", action="store_true", help="真实执行网络请求和 Claude Code 测试；不传时仅预览。")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="qwen2API 根地址，用于 /v1/models。")
    parser.add_argument("--anthropic-base-url", default="", help="Anthropic 兼容入口；为空时使用 <api-base>/anthropic。")
    parser.add_argument(
        "--api-key-env",
        dest="api_key_envs",
        action="append",
        default=[],
        help="读取 API Key 的环境变量名；可重复传入。",
    )
    parser.add_argument("--models", default="", help="逗号分隔的模型列表；不传则从 /v1/models 拉取。")
    parser.add_argument("--model-file", default="", help="从文件读取模型列表，每行一个模型。")
    parser.add_argument("--modes", default=",".join(sorted(TEXT_MODEL_MODES)), help="从 /v1/models 选择的模式。")
    parser.add_argument("--max-models", type=int, default=0, help="最多测试多少个模型，0 表示不限制。")
    parser.add_argument("--include-aliases", action="store_true", help="额外测试 Claude Code 常用 Claude 模型别名。")
    parser.add_argument("--include-p1", action="store_true", help="在 P0 全部通过的模型上继续执行 P1 场景。")
    parser.add_argument("--tests", default="", help="逗号分隔的场景名过滤，例如 Read,Write,Bash。")
    parser.add_argument("--result-dir", default="", help="结果目录；默认创建 /tmp/qwen2api-claude-tool-matrix-*。")
    parser.add_argument("--claude-bin", default="claude", help="Claude Code 命令路径。")
    parser.add_argument("--timeout", type=int, default=180, help="单场景默认超时时间。")
    parser.add_argument("--retries", type=int, default=1, help="失败后重试次数。")
    parser.add_argument("--permission-mode", default="acceptEdits", help="Claude Code 权限模式。")
    parser.add_argument("--isolation-mode", choices=("safe", "bare", "normal"), default="safe", help="Claude Code 启动隔离模式：safe 保留内置工具并禁用自定义项；bare 工具最少；normal 使用完整本机配置。")
    parser.add_argument("--verbose", action="store_true", help="输出每个场景进度。")

    args = parser.parse_args()
    if not args.api_key_envs:
        args.api_key_envs = list(DEFAULT_API_KEY_ENVS)
    return args


def read_api_key(env_names: Iterable[str]) -> str:
    """按顺序从环境变量读取 API Key，避免明文进入命令行参数。"""
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def make_default_result_dir() -> str:
    """生成默认结果目录路径。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"{random.randint(1000, 9999)}"
    return f"/tmp/qwen2api-claude-tool-matrix-{stamp}-{suffix}"


def resolve_models(args: argparse.Namespace, api_key: str) -> list[ModelEntry]:
    """根据命令行参数解析待测模型列表。"""
    explicit_models = parse_model_sources(args.models, args.model_file)
    if explicit_models:
        models = [ModelEntry(model_id=item, mode="manual", source="manual") for item in explicit_models]
    elif args.execute:
        modes = {item.strip() for item in args.modes.split(",") if item.strip()}
        models = fetch_models(api_base=args.api_base, api_key=api_key, modes=modes)
    else:
        # 预览模式不请求线上接口，只使用一个示例模型说明执行计划。
        models = [ModelEntry(model_id="qwen3.6-plus", mode="chat", base_model="qwen3.6-plus", source="preview")]

    if args.include_aliases:
        existing = {item.model_id for item in models}
        for alias in DEFAULT_ALIASES:
            if alias not in existing:
                models.append(ModelEntry(model_id=alias, mode="alias", source="alias"))

    if args.max_models > 0:
        models = models[: args.max_models]
    return models


def parse_model_sources(models_arg: str, model_file: str) -> list[str]:
    """解析手工传入的模型列表。"""
    models: list[str] = []
    if models_arg.strip():
        models.extend(item.strip() for item in models_arg.split(",") if item.strip())
    if model_file:
        path = pathlib.Path(model_file)
        if not path.exists():
            raise SystemExit(f"模型文件不存在：{path}")
        models.extend(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#"))
    return dedupe(models)


def fetch_models(api_base: str, api_key: str, modes: set[str]) -> list[ModelEntry]:
    """从 qwen2API /v1/models 拉取并过滤文本类模型。"""
    url = api_base.rstrip("/") + "/v1/models"
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"拉取模型失败：HTTP {exc.code} {detail}") from exc
    except Exception as exc:  # noqa: BLE001 - 命令行工具需要给出统一错误
        raise SystemExit(f"拉取模型失败：{exc}") from exc

    entries: list[ModelEntry] = []
    for item in payload.get("data", []):
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        mode = str(item.get("mode") or "").strip()
        if not model_id or mode not in modes:
            continue
        entries.append(
            ModelEntry(
                model_id=model_id,
                mode=mode,
                base_model=str(item.get("base_model") or ""),
                source="/v1/models",
            )
        )
    return entries


def build_scenarios(include_p1: bool) -> list[Scenario]:
    """构建 P0/P1 工具调用场景列表。"""
    scenarios = [
        scenario_ls(),
        scenario_glob(),
        scenario_grep(),
        scenario_read(),
        scenario_write(),
        scenario_edit(),
        scenario_multiedit(),
        scenario_bash(),
        scenario_core_chain(),
        scenario_p0_chain(),
    ]
    if include_p1:
        scenarios.extend([
            scenario_todowrite(),
            scenario_notebook_edit(),
            scenario_webfetch(),
            scenario_parallel_read(),
        ])
    return scenarios


def filter_scenarios(scenarios: list[Scenario], tests_arg: str) -> list[Scenario]:
    """按场景名过滤测试列表。"""
    if not tests_arg.strip():
        return scenarios
    selected = {item.strip() for item in tests_arg.split(",") if item.strip()}
    result = [scenario for scenario in scenarios if scenario.name in selected]
    missing = selected - {scenario.name for scenario in result}
    if missing:
        raise SystemExit(f"未知场景：{', '.join(sorted(missing))}")
    return result


def run_model_matrix(
    *,
    args: argparse.Namespace,
    result_root: pathlib.Path,
    model: ModelEntry,
    scenarios: list[Scenario],
    env_api_key: str,
    fp: Any,
    discovered_tools: list[str],
) -> list[ScenarioResult]:
    """执行单个模型的所有场景，P1 只在 P0 通过后继续。"""
    results: list[ScenarioResult] = []
    p0_failed = False
    discovered_tool_set = set(discovered_tools)
    for scenario in scenarios:
        global_unavailable = [tool for tool in scenario.tools if discovered_tool_set and tool not in discovered_tool_set]
        if global_unavailable:
            result = ScenarioResult(
                model=model.model_id,
                scenario=scenario.name,
                phase=scenario.phase,
                status=SKIP,
                reason="当前 Claude Code 隔离模式未暴露目标工具：" + ", ".join(global_unavailable),
                duration_seconds=0.0,
                attempts=0,
                case_dir="",
                tools_seen=[],
                stdout_path="",
                stderr_path="",
                metadata={"discovered_tools": discovered_tools},
            )
            emit_result(fp, result)
            results.append(result)
            continue

        if scenario.phase == "P1" and p0_failed:
            result = ScenarioResult(
                model=model.model_id,
                scenario=scenario.name,
                phase=scenario.phase,
                status=SKIP,
                reason="P0 未全部通过，跳过 P1 场景",
                duration_seconds=0.0,
                attempts=0,
                case_dir="",
                tools_seen=[],
                stdout_path="",
                stderr_path="",
            )
            emit_result(fp, result)
            results.append(result)
            continue

        result = run_scenario_with_retries(
            args=args,
            result_root=result_root,
            model=model,
            scenario=scenario,
            env_api_key=env_api_key,
        )
        emit_result(fp, result)
        results.append(result)
        if scenario.phase == "P0" and result.status in {FAIL, PARTIAL, UNSTABLE}:
            p0_failed = True
    return results


def run_scenario_with_retries(
    *,
    args: argparse.Namespace,
    result_root: pathlib.Path,
    model: ModelEntry,
    scenario: Scenario,
    env_api_key: str,
) -> ScenarioResult:
    """执行场景并在失败时有限重试。"""
    attempts = args.retries + 1
    attempt_results: list[ScenarioResult] = []
    for attempt in range(1, attempts + 1):
        result = run_one_scenario(
            args=args,
            result_root=result_root,
            model=model,
            scenario=scenario,
            env_api_key=env_api_key,
            attempt=attempt,
        )
        attempt_results.append(result)
        if args.verbose:
            print(f"[{result.status}] {model.model_id}::{scenario.name} attempt={attempt} reason={result.reason}")
        if result.status == PASS:
            if attempt > 1:
                result.metadata["previous_attempt_statuses"] = [item.status for item in attempt_results[:-1]]
            return result

    last = attempt_results[-1]
    unique_statuses = {item.status for item in attempt_results}
    unique_reasons = {item.reason for item in attempt_results}
    if len(unique_statuses) > 1 or len(unique_reasons) > 1:
        last.status = UNSTABLE
        last.reason = "多次尝试结果不一致：" + "; ".join(f"{item.status}:{item.reason}" for item in attempt_results)
    return last


def run_one_scenario(
    *,
    args: argparse.Namespace,
    result_root: pathlib.Path,
    model: ModelEntry,
    scenario: Scenario,
    env_api_key: str,
    attempt: int,
) -> ScenarioResult:
    """执行单个模型的单个测试场景。"""
    safe_model = safe_name(model.model_id)
    safe_scenario = safe_name(scenario.name)
    case_dir = result_root / "cases" / safe_model / safe_scenario / f"attempt-{attempt}"
    case_dir.mkdir(parents=True, exist_ok=True)

    state: dict[str, Any] = {}
    stdout_path = case_dir / "claude.stdout.jsonl"
    stderr_path = case_dir / "claude.stderr.log"
    try:
        # 先准备种子文件和临时服务，确保模型只能在隔离目录内操作。
        state = scenario.seed(case_dir)
        prompt = scenario.prompt(case_dir, state)
        command = build_claude_command(args, model.model_id, scenario, case_dir, prompt)
        command_result = run_claude_command(
            command=command,
            env=build_claude_env(args, env_api_key, case_dir / ".claude-config"),
            cwd=case_dir,
            timeout_seconds=scenario.timeout_seconds or args.timeout,
        )
        stdout_path.write_text(command_result.stdout, encoding="utf-8")
        stderr_path.write_text(command_result.stderr, encoding="utf-8")

        events = parse_stream_json(command_result.stdout)
        unavailable = [tool for tool in scenario.tools if tool not in events.available_tools]
        if unavailable:
            metadata = {
                "available_tools": events.available_tools,
                "returncode": command_result.returncode,
                "timed_out": command_result.timed_out,
                "result_subtype": events.result_subtype,
                "parse_errors": events.parse_errors,
            }
            return ScenarioResult(
                model=model.model_id,
                scenario=scenario.name,
                phase=scenario.phase,
                status=SKIP,
                reason="当前 Claude Code 初始化未暴露目标工具：" + ", ".join(unavailable),
                duration_seconds=command_result.duration_seconds,
                attempts=attempt,
                case_dir=str(case_dir),
                tools_seen=events.tool_names(),
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
                metadata=metadata,
            )
        status, reason, metadata = scenario.validate(case_dir, state, events, command_result)
        metadata.update({
            "returncode": command_result.returncode,
            "timed_out": command_result.timed_out,
            "result_subtype": events.result_subtype,
            "parse_errors": events.parse_errors,
        })
        if command_result.returncode != 0 and status == PASS:
            status = PARTIAL
            reason = f"工具校验通过，但 Claude Code 退出码为 {command_result.returncode}"

        return ScenarioResult(
            model=model.model_id,
            scenario=scenario.name,
            phase=scenario.phase,
            status=status,
            reason=reason,
            duration_seconds=command_result.duration_seconds,
            attempts=attempt,
            case_dir=str(case_dir),
            tools_seen=events.tool_names(),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001 - 每个场景需要隔离失败
        return ScenarioResult(
            model=model.model_id,
            scenario=scenario.name,
            phase=scenario.phase,
            status=FAIL,
            reason=f"场景执行异常：{exc}",
            duration_seconds=0.0,
            attempts=attempt,
            case_dir=str(case_dir),
            tools_seen=[],
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
    finally:
        if scenario.cleanup is not None:
            with contextlib.suppress(Exception):
                scenario.cleanup(state)


def build_claude_command(
    args: argparse.Namespace,
    model_id: str,
    scenario: Scenario,
    case_dir: pathlib.Path,
    prompt: str,
) -> list[str]:
    """生成 Claude Code 子进程命令。"""
    tool_csv = ",".join(scenario.tools)
    command = [
        args.claude_bin,
        "--print",
        "--verbose",
    ]
    if args.isolation_mode == "safe":
        command.append("--safe-mode")
    elif args.isolation_mode == "bare":
        command.append("--bare")
    command.extend([
        "--model",
        model_id,
        "--output-format",
        "stream-json",
        "--permission-mode",
        args.permission_mode,
        "--add-dir",
        str(case_dir),
        f"--tools={tool_csv}",
        f"--allowedTools={tool_csv}",
        "--",
        prompt,
    ])
    return command


def build_claude_env(args: argparse.Namespace, api_key: str, config_dir: pathlib.Path) -> dict[str, str]:
    """隔离 Claude Code 配置并通过环境变量传递 API 地址与密钥。"""
    env = os.environ.copy()
    anthropic_base = args.anthropic_base_url.strip() or args.api_base.rstrip("/") + "/anthropic"
    config_dir.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["ANTHROPIC_BASE_URL"] = anthropic_base
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env["ANTHROPIC_API_KEY"] = api_key
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def coerce_text(value: Any) -> str:
    """把 subprocess 可能返回的 bytes/None 统一转成字符串。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def run_claude_command(command: list[str], env: dict[str, str], cwd: pathlib.Path, timeout_seconds: int) -> CommandResult:
    """运行 Claude Code 并采集 stdout/stderr。"""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            returncode=124,
            stdout=coerce_text(exc.stdout),
            stderr=coerce_text(exc.stderr),
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )


def parse_stream_json(stdout: str) -> EventSummary:
    """解析 Claude Code stream-json 输出，提取工具调用和最终文本。"""
    summary = EventSummary()
    assistant_turn = 0
    for event_index, line in enumerate(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            summary.parse_errors += 1
            continue
        summary.raw_event_count += 1

        if event.get("subtype") == "init" and isinstance(event.get("tools"), list):
            summary.available_tools = [str(item) for item in event.get("tools", [])]

        event_type = event.get("type")
        if event_type == "assistant":
            assistant_turn += 1
            message = event.get("message") if isinstance(event.get("message"), dict) else event
            calls = extract_tool_calls_from_message(message, event_index, assistant_turn)
            summary.tool_calls.extend(calls)
            summary.assistant_text += extract_text_from_message(message)
        elif event_type == "result":
            summary.result_subtype = str(event.get("subtype") or "")
            if isinstance(event.get("result"), str):
                summary.final_text += event["result"]
        else:
            # 部分 Claude Code 版本可能直接在事件顶层携带 tool_use。
            summary.tool_calls.extend(extract_tool_calls_recursive(event, event_index, assistant_turn))
    return summary


def extract_tool_calls_from_message(message: dict[str, Any], event_index: int, assistant_turn: int) -> list[ToolCall]:
    """从 assistant message.content 中提取工具调用。"""
    calls: list[ToolCall] = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use" and part.get("name"):
                calls.append(
                    ToolCall(
                        name=str(part.get("name")),
                        input=part.get("input") if isinstance(part.get("input"), dict) else {},
                        call_id=str(part.get("id") or ""),
                        event_index=event_index,
                        assistant_turn=assistant_turn,
                    )
                )
    if calls:
        return calls
    return extract_tool_calls_recursive(message, event_index, assistant_turn)


def extract_tool_calls_recursive(value: Any, event_index: int, assistant_turn: int) -> list[ToolCall]:
    """递归兜底提取工具调用，兼容不同 Claude Code JSON 事件形态。"""
    calls: list[ToolCall] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "tool_use" and node.get("name"):
                input_data = node.get("input") if isinstance(node.get("input"), dict) else {}
                key = json.dumps([node.get("id"), node.get("name"), input_data], sort_keys=True, ensure_ascii=False)
                if key not in seen:
                    seen.add(key)
                    calls.append(
                        ToolCall(
                            name=str(node.get("name")),
                            input=input_data,
                            call_id=str(node.get("id") or ""),
                            event_index=event_index,
                            assistant_turn=assistant_turn,
                        )
                    )
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return calls


def extract_text_from_message(message: dict[str, Any]) -> str:
    """从 assistant message.content 中提取可见文本。"""
    texts: list[str] = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
    elif isinstance(content, str):
        texts.append(content)
    return "\n".join(texts)


def final_text(events: EventSummary) -> str:
    """合并最终 result 和 assistant 文本，便于断言。"""
    return "\n".join(part for part in (events.final_text, events.assistant_text) if part)


def scenario_ls() -> Scenario:
    """构造 LS 场景。"""
    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        src = case_dir / "src"
        src.mkdir()
        (src / "a.txt").write_text("alpha\n", encoding="utf-8")
        (src / "b.txt").write_text("beta\n", encoding="utf-8")
        return {"src": src}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return textwrap.dedent(f"""
        Use the LS tool to inspect this directory first: {state['src']}.
        Do not use any other tool. After LS returns, answer exactly which .txt files are present.
        """).strip()

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        text = final_text(events)
        path_value = json.dumps(events.first_tool_input("LS"), ensure_ascii=False)
        if not events.has_tool("LS"):
            return FAIL, "未观察到 LS 工具调用", {}
        if "src" not in path_value:
            return PARTIAL, "LS 被调用，但参数未指向 src 目录", {"ls_input": path_value}
        if "a.txt" in text and "b.txt" in text:
            return PASS, "LS 调用和结果消费正常", {}
        return PARTIAL, "LS 已调用，但最终回答未同时引用 a.txt 和 b.txt", {"final_text": text[-500:]}

    return Scenario("LS", "P0", ("LS",), seed, prompt, validate)


def scenario_glob() -> Scenario:
    """构造 Glob 场景。"""
    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        (case_dir / "src").mkdir()
        (case_dir / "src" / "a.txt").write_text("alpha\n", encoding="utf-8")
        (case_dir / "src" / "b.md").write_text("beta\n", encoding="utf-8")
        (case_dir / "notes.txt").write_text("root note\n", encoding="utf-8")
        return {}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return "Use the Glob tool to find **/*.txt under the workspace. Do not use Bash. Then list the matching filenames."

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        text = final_text(events)
        glob_input = json.dumps(events.first_tool_input("Glob"), ensure_ascii=False)
        if not events.has_tool("Glob"):
            return FAIL, "未观察到 Glob 工具调用", {}
        if "*.txt" not in glob_input:
            return PARTIAL, "Glob 参数未包含 txt 匹配模式", {"glob_input": glob_input}
        if "a.txt" in text and "notes.txt" in text:
            return PASS, "Glob 调用和结果消费正常", {}
        return PARTIAL, "Glob 已调用，但最终回答未完整引用匹配文件", {"final_text": text[-500:]}

    return Scenario("Glob", "P0", ("Glob",), seed, prompt, validate)


def scenario_grep() -> Scenario:
    """构造 Grep 场景。"""
    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        src = case_dir / "src"
        src.mkdir()
        (src / "marker.txt").write_text("prefix QWEN_TOOL_PROBE_MAGIC suffix\n", encoding="utf-8")
        (src / "other.txt").write_text("nothing here\n", encoding="utf-8")
        return {"magic": "QWEN_TOOL_PROBE_MAGIC"}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return f"Use the Grep tool to find the token {state['magic']} in the workspace. Do not use Bash. Answer with the filename that contains it."

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        text = final_text(events)
        grep_input = json.dumps(events.first_tool_input("Grep"), ensure_ascii=False)
        if not events.has_tool("Grep"):
            return FAIL, "未观察到 Grep 工具调用", {}
        if state["magic"] not in grep_input:
            return PARTIAL, "Grep 参数未包含目标 token", {"grep_input": grep_input}
        if "marker.txt" in text:
            return PASS, "Grep 调用和结果消费正常", {}
        return PARTIAL, "Grep 已调用，但最终回答未引用 marker.txt", {"final_text": text[-500:]}

    return Scenario("Grep", "P0", ("Grep",), seed, prompt, validate)


def scenario_read() -> Scenario:
    """构造 Read 场景。"""
    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        secret = "READ_RESULT_42_" + hashlib.sha1(str(case_dir).encode()).hexdigest()[:8]
        path = case_dir / "secret.txt"
        path.write_text(f"secret-token={secret}\n", encoding="utf-8")
        return {"path": path, "secret": secret}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return f"Use the Read tool to read {state['path']}. Do not use Bash. Report only the secret-token value from the file."

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        text = final_text(events)
        if not events.has_tool("Read"):
            return FAIL, "未观察到 Read 工具调用", {}
        if state["secret"] in text:
            return PASS, "Read 调用和 tool_result 消费正常", {}
        return PARTIAL, "Read 已调用，但最终回答未包含文件中的 secret", {"final_text": text[-500:]}

    return Scenario("Read", "P0", ("Read",), seed, prompt, validate)


def scenario_write() -> Scenario:
    """构造 Write 场景。"""
    expected = '{\n  "message": "中文 hello",\n  "quote": "He said \\\"yes\\\"",\n  "xml": "<tag attr=\\\"1\\\">value</tag>"\n}'

    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        return {"path": case_dir / "probe-write.json", "expected": expected}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return textwrap.dedent(f"""
        Use the Write tool to create this exact file: {state['path']}
        The file content must be exactly:
        ```json
        {state['expected']}```
        Do not use Bash or Edit.
        """).strip()

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        path = state["path"]
        if not events.has_tool("Write"):
            return FAIL, "未观察到 Write 工具调用", {}
        if not path.exists():
            return FAIL, "Write 已调用但目标文件不存在", {}
        actual = path.read_text(encoding="utf-8")
        if actual == state["expected"]:
            return PASS, "Write 写入内容精确匹配", {}
        return FAIL, "Write 文件内容不匹配", {"expected_sha256": sha256_text(state["expected"]), "actual_sha256": sha256_text(actual), "actual": actual}

    return Scenario("Write", "P0", ("Write",), seed, prompt, validate)


def scenario_edit() -> Scenario:
    """构造 Edit 场景。"""
    before = "line-1\nold-value=EDIT_TARGET_OLD\nline-3\n"
    after = "line-1\nold-value=EDIT_TARGET_NEW\nline-3\n"

    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        path = case_dir / "edit-target.txt"
        path.write_text(before, encoding="utf-8")
        return {"path": path, "after": after}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return f"First use Read on {state['path']} because Claude Code requires reading before editing. Then use Edit to replace EDIT_TARGET_OLD with EDIT_TARGET_NEW. Do not use Write or Bash."

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        if not events.has_tool("Read"):
            return FAIL, "Edit 前未观察到 Read 工具调用", {"tools_seen": events.tool_names()}
        if not events.has_tool("Edit"):
            return FAIL, "未观察到 Edit 工具调用", {"tools_seen": events.tool_names()}
        actual = state["path"].read_text(encoding="utf-8")
        if actual == state["after"]:
            return PASS, "Edit 精确替换成功", {}
        return FAIL, "Edit 后文件内容不符合预期", {"actual": actual}

    return Scenario("Edit", "P0", ("Read", "Edit"), seed, prompt, validate)


def scenario_multiedit() -> Scenario:
    """构造 MultiEdit 场景。"""
    before = "A=PLACEHOLDER_A\nB=PLACEHOLDER_B\nC=PLACEHOLDER_C\n"
    after = "A=VALUE_A\nB=VALUE_B\nC=VALUE_C\n"

    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        path = case_dir / "multi-edit-target.txt"
        path.write_text(before, encoding="utf-8")
        return {"path": path, "after": after}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return textwrap.dedent(f"""
        Use exactly one MultiEdit tool call to update {state['path']}:
        - PLACEHOLDER_A -> VALUE_A
        - PLACEHOLDER_B -> VALUE_B
        - PLACEHOLDER_C -> VALUE_C
        Do not use Bash, Write, or separate Edit calls.
        """).strip()

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        if not events.has_tool("MultiEdit"):
            return FAIL, "未观察到 MultiEdit 工具调用", {"tools_seen": events.tool_names()}
        actual = state["path"].read_text(encoding="utf-8")
        if actual == state["after"]:
            return PASS, "MultiEdit 多处替换成功", {}
        return FAIL, "MultiEdit 后文件内容不符合预期", {"actual": actual}

    return Scenario("MultiEdit", "P0", ("MultiEdit",), seed, prompt, validate)


def scenario_bash() -> Scenario:
    """构造 Bash 场景。"""
    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        secret = "BASH_RESULT_" + hashlib.sha1(str(case_dir).encode()).hexdigest()[:10]
        path = case_dir / "bash-target.txt"
        path.write_text(secret + "\n", encoding="utf-8")
        return {"path": path, "secret": secret}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return textwrap.dedent(f"""
        Use the Bash tool from the workspace directory to print the content of bash-target.txt.
        Do not use Read. After the command returns, answer with the printed value.
        Workspace directory: {case_dir}
        """).strip()

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        text = final_text(events)
        bash_input = events.first_tool_input("Bash")
        command = str(bash_input.get("command") or "")
        if not events.has_tool("Bash"):
            return FAIL, "未观察到 Bash 工具调用", {}
        if is_risky_shell_command(command, case_dir):
            return FAIL, "Bash 命令疑似越界或高风险", {"command": command}
        if state["secret"] in text:
            return PASS, "Bash 调用和输出消费正常", {"command": command}
        return PARTIAL, "Bash 已调用，但最终回答未包含命令输出", {"command": command, "final_text": text[-500:]}

    return Scenario("Bash", "P0", ("Bash",), seed, prompt, validate)


def scenario_core_chain() -> Scenario:
    """构造当前 Claude Code 可用核心工具链路场景。"""
    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        secret = "CORE_SECRET_" + hashlib.sha1(str(case_dir).encode()).hexdigest()[:8]
        target = case_dir / "core-target.txt"
        target.write_text(f"status=CORE_OLD\nsecret={secret}\n", encoding="utf-8")
        report = case_dir / "core-report.txt"
        marker = "CORE_CHAIN_OK_" + secret.rsplit("_", 1)[-1]
        return {"target": target, "report": report, "secret": secret, "marker": marker}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return textwrap.dedent(f"""
        Complete this workflow using tools only where requested:
        1. Use Read on {state['target']} and remember the secret value from the file.
        2. Use Edit to replace CORE_OLD with CORE_NEW in {state['target']}.
        3. Use Write to create {state['report']} with exactly: report={state['secret']}
        4. Use Bash to verify both files, with this exact command:
           python3 -c "from pathlib import Path; assert 'CORE_NEW' in Path('core-target.txt').read_text(); assert Path('core-report.txt').read_text() == 'report={state['secret']}'; print('{state['marker']}')"
        5. After Bash returns, answer with exactly the printed marker.
        Do not skip any of the four tool types.
        """).strip()

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        required = {"Read", "Edit", "Write", "Bash"}
        seen = set(events.tool_names())
        missing = sorted(required - seen)
        target_ok = "CORE_NEW" in state["target"].read_text(encoding="utf-8")
        report_ok = state["report"].exists() and state["report"].read_text(encoding="utf-8") == f"report={state['secret']}"
        text_ok = state["marker"] in final_text(events)
        if missing:
            return FAIL, "CoreChain 缺少工具调用：" + ", ".join(missing), {"seen": sorted(seen)}
        if target_ok and report_ok and text_ok:
            return PASS, "CoreChain 核心工具链成功", {}
        status = PARTIAL if target_ok and report_ok else FAIL
        return status, "CoreChain 工具齐全但副作用或最终回答不完整", {
            "target_ok": target_ok,
            "report_ok": report_ok,
            "text_ok": text_ok,
            "final_text": final_text(events)[-500:],
        }

    return Scenario("CoreChain", "P0", ("Read", "Edit", "Write", "Bash"), seed, prompt, validate, timeout_seconds=240)


def scenario_p0_chain() -> Scenario:
    """构造 P0 多工具链路场景。"""
    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        src = case_dir / "src"
        src.mkdir()
        target = src / "chain-target.txt"
        target.write_text("status=OLD_CHAIN_VALUE\nmarker=CHAIN_MAGIC_TOKEN\n", encoding="utf-8")
        (src / "chain-other.txt").write_text("marker=OTHER\n", encoding="utf-8")
        report = case_dir / "chain-report.txt"
        return {"src": src, "target": target, "report": report}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return textwrap.dedent(f"""
        Complete this workflow using tools, in order:
        1. Use LS on {state['src']}.
        2. Use Glob to find src/*.txt.
        3. Use Grep to locate CHAIN_MAGIC_TOKEN.
        4. Use Read on the located target file.
        5. Use Edit to replace OLD_CHAIN_VALUE with NEW_CHAIN_VALUE in that file.
        6. Use Write to create {state['report']} with exactly: chain=ok
        7. Use Bash to verify the target file contains NEW_CHAIN_VALUE and the report file contains chain=ok.
        Finish with CHAIN_WORKFLOW_OK only after the Bash verification succeeds.
        """).strip()

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        required = {"LS", "Glob", "Grep", "Read", "Edit", "Write", "Bash"}
        seen = set(events.tool_names())
        missing = sorted(required - seen)
        target_ok = "NEW_CHAIN_VALUE" in state["target"].read_text(encoding="utf-8")
        report_ok = state["report"].exists() and state["report"].read_text(encoding="utf-8") == "chain=ok\n"
        text_ok = "CHAIN_WORKFLOW_OK" in final_text(events)
        if missing:
            return FAIL, "组合链路缺少工具调用：" + ", ".join(missing), {"seen": sorted(seen)}
        if target_ok and report_ok and text_ok:
            return PASS, "P0 多工具链路成功", {}
        return PARTIAL, "P0 多工具链路工具齐全但副作用或最终回答不完整", {"target_ok": target_ok, "report_ok": report_ok, "text_ok": text_ok}

    return Scenario("P0Chain", "P0", ("LS", "Glob", "Grep", "Read", "Edit", "Write", "Bash"), seed, prompt, validate, timeout_seconds=240)


def scenario_todowrite() -> Scenario:
    """构造 TodoWrite 场景。"""
    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        return {}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return "Use TodoWrite to create exactly three todos for this test. Mark the first completed, the second in_progress, and the third pending. Then summarize the statuses."

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        if not events.has_tool("TodoWrite"):
            return FAIL, "未观察到 TodoWrite 工具调用", {}
        payload = events.first_tool_input("TodoWrite")
        todos = payload.get("todos") if isinstance(payload.get("todos"), list) else []
        statuses = {str(item.get("status")) for item in todos if isinstance(item, dict)}
        if len(todos) >= 3 and {"completed", "in_progress", "pending"}.issubset(statuses):
            return PASS, "TodoWrite 嵌套参数结构正常", {"todo_count": len(todos), "statuses": sorted(statuses)}
        return PARTIAL, "TodoWrite 已调用，但 todos 结构或状态不完整", {"payload": payload}

    return Scenario("TodoWrite", "P1", ("TodoWrite",), seed, prompt, validate)


def scenario_notebook_edit() -> Scenario:
    """构造 NotebookEdit 场景。"""
    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        path = case_dir / "probe.ipynb"
        notebook = {
            "cells": [
                {
                    "cell_type": "markdown",
                    "metadata": {},
                    "source": ["# Original\n"],
                }
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        path.write_text(json.dumps(notebook, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": path, "marker": "NOTEBOOK_EDIT_OK"}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return f"Use NotebookEdit to add a new markdown cell containing exactly {state['marker']} to {state['path']}. Do not use Bash."

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        if not events.has_tool("NotebookEdit"):
            return FAIL, "未观察到 NotebookEdit 工具调用", {"tools_seen": events.tool_names()}
        try:
            notebook = json.loads(state["path"].read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return FAIL, f"NotebookEdit 后 ipynb JSON 无效：{exc}", {}
        joined = json.dumps(notebook, ensure_ascii=False)
        if state["marker"] in joined:
            return PASS, "NotebookEdit 修改后 notebook JSON 有效且包含 marker", {}
        return PARTIAL, "NotebookEdit 已调用，但 notebook 未包含目标 marker", {"notebook": notebook}

    return Scenario("NotebookEdit", "P1", ("NotebookEdit",), seed, prompt, validate)


def scenario_webfetch() -> Scenario:
    """构造 WebFetch 场景，访问本地临时 HTTP 服务。"""
    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        web_root = case_dir / "web-root"
        web_root.mkdir()
        marker = "WEBFETCH_LOCAL_OK_" + hashlib.sha1(str(case_dir).encode()).hexdigest()[:8]
        (web_root / "probe.txt").write_text(marker + "\n", encoding="utf-8")
        server, thread, port = start_local_http_server(web_root)
        return {"server": server, "thread": thread, "port": port, "marker": marker}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        url = f"http://127.0.0.1:{state['port']}/probe.txt"
        return f"Use WebFetch to fetch {url}. Then answer with the exact marker returned by the page. Do not use Bash."

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        text = final_text(events)
        if not events.has_tool("WebFetch"):
            return FAIL, "未观察到 WebFetch 工具调用", {}
        if state["marker"] in text:
            return PASS, "WebFetch 本地访问和结果消费正常", {}
        return PARTIAL, "WebFetch 已调用，但最终回答未包含本地 marker", {"final_text": text[-500:]}

    def cleanup(state: dict[str, Any]) -> None:
        server = state.get("server")
        if server is not None:
            server.shutdown()
            server.server_close()

    return Scenario("WebFetch", "P1", ("WebFetch",), seed, prompt, validate, cleanup=cleanup)


def scenario_parallel_read() -> Scenario:
    """构造同轮多工具调用场景。"""
    def seed(case_dir: pathlib.Path) -> dict[str, Any]:
        a = case_dir / "parallel-a.txt"
        b = case_dir / "parallel-b.txt"
        a.write_text("PARALLEL_A_VALUE\n", encoding="utf-8")
        b.write_text("PARALLEL_B_VALUE\n", encoding="utf-8")
        return {"a": a, "b": b}

    def prompt(case_dir: pathlib.Path, state: dict[str, Any]) -> str:
        return textwrap.dedent(f"""
        In your next assistant turn, issue two Read tool calls in parallel: one for {state['a']} and one for {state['b']}.
        After both tool results return, answer with both values. Do not use Bash.
        """).strip()

    def validate(case_dir: pathlib.Path, state: dict[str, Any], events: EventSummary, result: CommandResult) -> tuple[str, str, dict[str, Any]]:
        text = final_text(events)
        read_count = sum(1 for name in events.tool_names() if name == "Read")
        same_turn = events.max_same_turn_count("Read")
        if read_count < 2:
            return FAIL, "未观察到至少两个 Read 工具调用", {"read_count": read_count, "tools_seen": events.tool_names()}
        if "PARALLEL_A_VALUE" in text and "PARALLEL_B_VALUE" in text and same_turn >= 2:
            return PASS, "同轮多 Read 工具调用成功", {"read_count": read_count, "same_turn": same_turn}
        if "PARALLEL_A_VALUE" in text and "PARALLEL_B_VALUE" in text:
            return PARTIAL, "两个 Read 成功但未确认同一 assistant turn 并行发出", {"read_count": read_count, "same_turn": same_turn}
        return PARTIAL, "Read 工具调用完成但最终回答未包含两个值", {"read_count": read_count, "same_turn": same_turn, "final_text": text[-500:]}

    return Scenario("ParallelRead", "P1", ("Read",), seed, prompt, validate)


def start_local_http_server(root: pathlib.Path) -> tuple[ReusableThreadingHTTPServer, threading.Thread, int]:
    """启动仅监听 127.0.0.1 的临时 HTTP 服务。"""
    port = find_free_port()

    class Handler(QuietHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

    server = ReusableThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def find_free_port() -> int:
    """查找本机可用端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def is_risky_shell_command(command: str, case_dir: pathlib.Path) -> bool:
    """用保守规则检查 Bash 命令是否明显越界或高风险。"""
    lowered = command.lower()
    risky_tokens = ("rm -rf", "sudo", "curl ", "wget ", "ssh ", "scp ", "chmod -r", "chown -r", "> /etc/", " /etc/")
    if any(token in lowered for token in risky_tokens):
        return True
    if ".." in command:
        return True
    absolute_paths = re.findall(r"(?<![A-Za-z0-9_])/(?:[^\s'\"]+)", command)
    case_prefix = str(case_dir)
    for path in absolute_paths:
        # 允许解释器路径和测试目录路径，阻止明显操作其他绝对路径。
        if path.startswith(("/usr/bin/", "/bin/", "/opt/homebrew/", "/usr/local/bin/", case_prefix)):
            continue
        return True
    return False


def sha256_text(text: str) -> str:
    """计算文本 SHA256，避免摘要中输出大段内容。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_name(value: str) -> str:
    """将模型名或场景名转为安全路径片段。"""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120]


def dedupe(values: Iterable[str]) -> list[str]:
    """保持顺序去重。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def emit_result(fp: Any, result: ScenarioResult) -> None:
    """写入单条 JSONL 结果。"""
    fp.write(json.dumps(dataclasses.asdict(result), ensure_ascii=False) + "\n")
    fp.flush()


def discover_available_tools(args: argparse.Namespace, result_root: pathlib.Path) -> list[str]:
    """通过 Claude Code 初始化事件发现当前隔离模式实际可用工具。"""
    case_dir = result_root / "tool-discovery"
    case_dir.mkdir(parents=True, exist_ok=True)
    command = [
        args.claude_bin,
        "--print",
        "--verbose",
    ]
    if args.isolation_mode == "safe":
        command.append("--safe-mode")
    elif args.isolation_mode == "bare":
        command.append("--bare")
    command.extend([
        "--model",
        "qwen3.6-plus",
        "--output-format",
        "stream-json",
        "--permission-mode",
        args.permission_mode,
        "--add-dir",
        str(case_dir),
        "--",
        "Initialize only. Do not call tools.",
    ])

    env = build_claude_env(args, "sk-invalid-tool-discovery", case_dir / ".claude-config")
    result = run_claude_command(command=command, env=env, cwd=case_dir, timeout_seconds=30)
    (case_dir / "claude.stdout.jsonl").write_text(result.stdout, encoding="utf-8")
    (case_dir / "claude.stderr.log").write_text(result.stderr, encoding="utf-8")
    events = parse_stream_json(result.stdout)
    return events.available_tools


def write_manifest(result_root: pathlib.Path, args: argparse.Namespace, models: list[ModelEntry], scenarios: list[Scenario], discovered_tools: list[str]) -> None:
    """写入本次测试清单，避免记录敏感 API Key。"""
    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "execute": bool(args.execute),
        "api_base": args.api_base,
        "anthropic_base_url": args.anthropic_base_url or args.api_base.rstrip("/") + "/anthropic",
        "api_key_envs": args.api_key_envs,
        "models": [dataclasses.asdict(model) for model in models],
        "scenarios": [{"name": item.name, "phase": item.phase, "tools": list(item.tools)} for item in scenarios],
        "discovered_tools": discovered_tools,
        "include_p1": bool(args.include_p1),
        "include_aliases": bool(args.include_aliases),
        "isolation_mode": args.isolation_mode,
        "timeout": args.timeout,
        "retries": args.retries,
    }
    (result_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def print_preview(result_root: pathlib.Path, models: list[ModelEntry], scenarios: list[Scenario], args: argparse.Namespace) -> None:
    """打印安全预览信息。"""
    print(f"结果目录将使用：{result_root}")
    print(f"模型数量：{len(models)}")
    for model in models[:20]:
        print(f"  - {model.model_id} [{model.mode}/{model.source}]")
    if len(models) > 20:
        print(f"  ... 还有 {len(models) - 20} 个模型")
    print(f"场景数量：{len(scenarios)}")
    for scenario in scenarios:
        print(f"  - {scenario.phase} {scenario.name}: tools={','.join(scenario.tools)}")
    print("真实执行示例：")
    print("  ANTHROPIC_API_KEY=*** python3 tools/claude_tool_matrix.py --execute --include-p1 --include-aliases")


def write_summary(result_root: pathlib.Path, results: list[ScenarioResult]) -> pathlib.Path:
    """生成 Markdown 汇总报告。"""
    summary_path = result_root / "summary.md"
    by_status = defaultdict(int)
    by_model: dict[str, list[ScenarioResult]] = defaultdict(list)
    for result in results:
        by_status[result.status] += 1
        by_model[result.model].append(result)

    lines = [
        "# Claude Code 工具调用能力矩阵测试结果",
        "",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
        "## 总览",
        "",
    ]
    for status in (PASS, PARTIAL, FAIL, UNSTABLE, SKIP):
        lines.append(f"- {status}: {by_status[status]}")

    lines.extend(["", "## 模型结果", ""])
    for model, items in by_model.items():
        p0_items = [item for item in items if item.phase == "P0"]
        p1_items = [item for item in items if item.phase == "P1"]
        p0_effective = [item for item in p0_items if item.status != SKIP]
        model_pass = bool(p0_effective) and all(item.status == PASS for item in p0_effective)
        lines.append(f"### {model}")
        lines.append("")
        lines.append(f"- P0：{'通过' if model_pass else '未通过'}")
        if p1_items:
            lines.append(f"- P1：{sum(1 for item in p1_items if item.status == PASS)}/{len(p1_items)} 通过")
        lines.append("")
        lines.append("| 阶段 | 场景 | 状态 | 工具 | 原因 |")
        lines.append("|---|---|---|---|---|")
        for item in items:
            tools = ", ".join(item.tools_seen) if item.tools_seen else "-"
            reason = item.reason.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {item.phase} | {item.scenario} | {item.status} | {tools} | {reason} |")
        lines.append("")

    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


if __name__ == "__main__":
    raise SystemExit(main())
