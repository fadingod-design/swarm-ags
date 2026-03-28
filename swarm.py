# -*- coding: utf-8 -*-
"""
蜂群AGS V4.3 - 增强版

核心特性：
1. 角色完全自定义 - 通过配置文件定义任意角色
2. 工作流可配置 - 支持DAG工作流
3. 独立上下文 - 像sessions_spawn一样的独立会话
4. Agent间通信 - 支持消息传递、Blackboard
5. 动态切换角色 - 运行时可更换角色和模型
6. 联网搜索 - 研究员可搜索最新信息
7. 自动工具调用 - 根据任务类型自动调用工具
8. 增强质检 - 检查输出长度、真实数据、工具调用

V4.3 更新：
- 自动启动工具代理服务
- 增强质检标准（输出长度、真实数据检测）
- 自动调用工具（股票分析自动获取行情）
- 优化prompt，要求更详细的输出
"""

import json
import time
import threading
import traceback
import os
import urllib.request
import urllib.parse
import hashlib
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Dict, List, Any, Optional
from collections import defaultdict
import queue
import psutil  # 系统监控

# ========== 增强联网模块 ==========
try:
    from enhanced_search import EnhancedWebSearch
    _enhanced_search = EnhancedWebSearch()
    print("[EnhancedSearch] 增强联网模块加载成功")
except ImportError:
    _enhanced_search = None
    print("[EnhancedSearch] 增强联网模块未找到，使用默认搜索")


# 加载 .env 文件
def load_env(env_path: str = None):
    """加载环境变量文件"""
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    # 只设置未定义的环境变量
                    if key not in os.environ:
                        os.environ[key] = value

# 启动时加载环境变量
load_env()


# ========== 系统监控模块 ==========

class SystemMonitor:
    """系统资源监控 - CPU、内存、网络"""
    
    def __init__(self):
        self.last_net_io = psutil.net_io_counters()
        self.last_time = time.time()
        self.agent_network_usage = {}  # 记录每个agent的网络消耗
        self.agent_cpu_usage = {}  # 记录每个agent的CPU消耗
    
    def get_system_stats(self) -> dict:
        """获取系统整体状态"""
        # CPU
        cpu_percent = psutil.cpu_percent(interval=0.1)
        cpu_per_core = psutil.cpu_percent(interval=0.1, percpu=True)
        
        # 内存
        mem = psutil.virtual_memory()
        
        # 网络速率
        current_net = psutil.net_io_counters()
        current_time = time.time()
        time_diff = current_time - self.last_time
        
        if time_diff > 0:
            bytes_sent = current_net.bytes_sent - self.last_net_io.bytes_sent
            bytes_recv = current_net.bytes_recv - self.last_net_io.bytes_recv
            
            upload_speed = bytes_sent / time_diff  # bytes/s
            download_speed = bytes_recv / time_diff
        else:
            upload_speed = 0
            download_speed = 0
        
        self.last_net_io = current_net
        self.last_time = current_time
        
        return {
            "cpu": {
                "total": round(cpu_percent, 1),
                "cores": cpu_per_core
            },
            "memory": {
                "total": mem.total,
                "used": mem.used,
                "percent": mem.percent,
                "available": mem.available
            },
            "network": {
                "upload_speed": int(upload_speed),  # bytes/s
                "download_speed": int(download_speed),
                "upload_mb": round(upload_speed / 1024 / 1024, 2),  # MB/s
                "download_mb": round(download_speed / 1024 / 1024, 2)
            }
        }
    
    def track_agent_request(self, agent_id: str, tokens: int, duration: float):
        """跟踪agent的资源消耗"""
        if agent_id not in self.agent_network_usage:
            self.agent_network_usage[agent_id] = {
                "total_tokens": 0,
                "request_count": 0,
                "total_time": 0
            }
        
        self.agent_network_usage[agent_id]["total_tokens"] += tokens
        self.agent_network_usage[agent_id]["request_count"] += 1
        self.agent_network_usage[agent_id]["total_time"] += duration
        
        # 估算网络流量（tokens大约4字节/token）
        self.agent_network_usage[agent_id]["estimated_bytes"] = \
            self.agent_network_usage[agent_id]["total_tokens"] * 4
    
    def get_agent_stats(self) -> dict:
        """获取各agent的资源消耗统计"""
        return self.agent_network_usage.copy()
    
    def reset(self):
        """重置统计"""
        self.agent_network_usage = {}
        self.agent_cpu_usage = {}


# ========== 联网搜索模块（使用AutoGLM + MCP集成） ==========

class WebSearch:
    """联网搜索工具 - 使用AutoGLM AgentDR API（与autoglm-websearch skill一致）"""
    
    # AutoGLM配置 - 从环境变量读取
    APP_ID = os.environ.get("AUTOGLM_APP_ID", "")
    APP_KEY = os.environ.get("AUTOGLM_APP_KEY", "")
    SEARCH_API = "https://autoglm-api.zhipuai.cn/agentdr/v1/assistant/skills/web-search"
    
    def __init__(self, token_getter_url="http://127.0.0.1:53699/get_token"):
        self.token_url = token_getter_url
        self.web_fetch_api = "http://127.0.0.1:8765/api/web_fetch"
        
        # 检查必要的环境变量
        if not self.APP_ID or not self.APP_KEY:
            print("[WebSearch] 警告: AUTOGLM_APP_ID 或 AUTOGLM_APP_KEY 未设置")
    
    def get_token(self):
        """获取AutoGLM Token"""
        try:
            req = urllib.request.Request(self.token_url)
            with urllib.request.urlopen(req, timeout=5) as r:
                token = r.read().decode('utf-8').strip()
                # 确保格式为 "Bearer xxx"
                if not token.lower().startswith("bearer "):
                    token = f"Bearer {token}"
                return token
        except Exception as e:
            print(f"[WebSearch] Token获取失败: {e}")
            return ""
    
    def search(self, query: str, max_results: int = 10) -> dict:
        """执行网络搜索 - 优先使用增强版"""
        # 优先使用增强版搜索（多搜索引擎）
        global _enhanced_search
        if _enhanced_search:
            try:
                result = _enhanced_search.search(query, use_multi=True, max_results=max_results)
                if result.get("ok"):
                    return result
                print("[WebSearch] EnhancedSearch返回空结果，降级到AutoGLM")
            except Exception as e:
                print(f"[WebSearch] EnhancedSearch失败: {e}")
        
        # 降级到原始AutoGLM搜索
        token = self.get_token()
        if not token:
            return {"ok": False, "error": "无法获取搜索Token，请确保AutoGLM服务运行中"}
        
        try:
            # 生成签名（与autoglm-websearch skill一致）
            timestamp = str(int(time.time()))
            sign_data = f"{self.APP_ID}&{timestamp}&{self.APP_KEY}"
            sign = hashlib.md5(sign_data.encode('utf-8')).hexdigest()
            
            # 构建请求
            data = json.dumps({
                "queries": [{"query": query}]
            }).encode('utf-8')
            
            req = urllib.request.Request(
                self.SEARCH_API,
                data=data,
                headers={
                    "Authorization": token,
                    "Content-Type": "application/json",
                    "X-Auth-Appid": self.APP_ID,
                    "X-Auth-TimeStamp": timestamp,
                    "X-Auth-Sign": sign
                }
            )
            
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode('utf-8'))
                
                # 解析响应
                if resp.get("code") == 0 and resp.get("data", {}).get("results"):
                    results = []
                    for item in resp["data"]["results"]:
                        web_pages = item.get("webPages", {}).get("value", [])
                        for page in web_pages[:max_results]:
                            results.append({
                                "title": page.get("name", ""),
                                "url": page.get("url", ""),
                                "snippet": page.get("snippet", "")[:500]
                            })
                    
                    if results:
                        return {
                            "ok": True,
                            "query": query,
                            "results": results[:max_results],
                            "count": len(results[:max_results])
                        }
                
                return {"ok": False, "error": resp.get("msg", "未找到相关结果")}
        
        except Exception as e:
            return {"ok": False, "error": str(e)}
    
    def fetch_url(self, url: str) -> dict:
        """获取网页内容 - 使用MCP fetch服务或web_fetch"""
        try:
            # 尝试使用web_fetch API
            data = json.dumps({"url": url}).encode('utf-8')
            req = urllib.request.Request(
                self.web_fetch_api,
                data=data,
                headers={"Content-Type": "application/json"},
                method='POST'
            )
            
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode('utf-8'))
                if resp.get("content"):
                    return {
                        "ok": True,
                        "url": url,
                        "content": resp["content"][:5000]
                    }
        except:
            pass
        
        # 回退：直接获取网页
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as r:
                content = r.read().decode('utf-8', errors='ignore')
                return {
                    "ok": True,
                    "url": url,
                    "content": content[:5000]
                }
        except Exception as e:
            return {"ok": False, "error": str(e)}
    
    def format_results(self, search_result: dict) -> str:
        """格式化搜索结果"""
        if not search_result.get("ok"):
            return f"搜索失败: {search_result.get('error', '未知错误')}"
        
        results = search_result.get("results", [])
        if not results:
            return "未找到相关结果"
        
        output = f"[Search] 搜索关键词: {search_result.get('query', '')}\n"
        output += f"找到 {search_result.get('count', 0)} 条结果:\n\n"
        
        for i, r in enumerate(results, 1):
            output += f"【{i}】{r['title']}\n"
            output += f"摘要: {r['snippet']}\n"
            output += f"链接: {r['url']}\n\n"
        
        return output


# ========== 配置加载 ==========

class ConfigLoader:
    """配置加载器"""
    
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load()
    
    def _expand_env(self, value):
        """递归替换环境变量占位符 ${VAR}"""
        if isinstance(value, str):
            # 替换 ${VAR} 格式的占位符
            pattern = r'\$\{([^}]+)\}'
            def replace(match):
                var_name = match.group(1)
                env_value = os.environ.get(var_name, '')
                if not env_value:
                    print(f"[ConfigLoader] 警告: 环境变量 {var_name} 未设置")
                return env_value
            return re.sub(pattern, replace, value)
        elif isinstance(value, dict):
            return {k: self._expand_env(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._expand_env(item) for item in value]
        return value
    
    def _load(self) -> dict:
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")
        with open(self.config_path, 'r', encoding='utf-8') as f:
            raw_config = json.load(f)
        # 递归替换环境变量
        return self._expand_env(raw_config)
    
    def reload(self) -> dict:
        self.config = self._load()
        return self.config
    
    def get_providers(self) -> dict:
        return self.config.get("providers", {})
    
    def get_agents(self) -> dict:
        return self.config.get("agents", {})
    
    def get_workflows(self) -> dict:
        return self.config.get("workflows", {})
    
    def get_agent_config(self, agent_id: str) -> Optional[dict]:
        return self.config.get("agents", {}).get(agent_id)
    
    def get_workflow(self, workflow_id: str) -> Optional[dict]:
        return self.config.get("workflows", {}).get(workflow_id)
    
    def get_context_config(self) -> dict:
        return self.config.get("context", {})


# ========== 上下文管理 ==========

class AgentContext:
    """Agent独立上下文管理"""
    
    def __init__(self, agent_id: str, config: dict, global_config: dict):
        self.agent_id = agent_id
        self.messages: List[dict] = []
        self.max_tokens = config.get("max_tokens", global_config.get("default_max_tokens", 100000))
        self.keep_messages = global_config.get("keep_messages", 6)
        self.warning_threshold = global_config.get("warning_threshold", 0.7)
        self.compress_threshold = global_config.get("compress_threshold", 0.85)
    
    def set_system_prompt(self, prompt: str):
        self.messages = [{"role": "system", "content": prompt}]
    
    def add_message(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
    
    def count_tokens(self) -> int:
        total = 0
        for msg in self.messages:
            total += len(msg.get("content", "")) // 4
        return total
    
    def get_ratio(self) -> float:
        return self.count_tokens() / self.max_tokens
    
    def check_status(self) -> tuple:
        ratio = self.get_ratio()
        if ratio >= self.compress_threshold:
            return "critical", ratio
        elif ratio >= self.warning_threshold:
            return "warning", ratio
        return "ok", ratio
    
    def get_messages(self) -> List[dict]:
        return self.messages.copy()
    
    def clear(self):
        if self.messages and self.messages[0]["role"] == "system":
            self.messages = [self.messages[0]]
        else:
            self.messages = []


# ========== Blackboard ==========

class Blackboard:
    """共享信息板"""
    
    def __init__(self, config: dict):
        self.enabled = config.get("enabled", True)
        self.max_entries = config.get("max_entries", 100)
        self.data: Dict[str, dict] = {}
        self.subscribers: Dict[str, List[str]] = defaultdict(list)
        self.lock = threading.Lock()
    
    def write(self, source: str, key: str, value: Any):
        if not self.enabled:
            return
        with self.lock:
            full_key = f"{source}:{key}"
            self.data[full_key] = {
                "source": source,
                "key": key,
                "value": value,
                "timestamp": time.time()
            }
            if len(self.data) > self.max_entries:
                oldest = min(self.data.items(), key=lambda x: x[1]["timestamp"])
                del self.data[oldest[0]]
    
    def read(self, source: str = None, key: str = None) -> Any:
        with self.lock:
            if source and key:
                full_key = f"{source}:{key}"
                entry = self.data.get(full_key)
                return entry["value"] if entry else None
            elif source:
                return {k: v for k, v in self.data.items() if v["source"] == source}
            else:
                return {k: v["value"] for k, v in self.data.items()}
    
    def subscribe(self, agent_id: str, sources: List[str]):
        with self.lock:
            self.subscribers[agent_id] = sources
    
    def get_agent_context(self, agent_id: str) -> str:
        sources = self.subscribers.get(agent_id, [])
        if not sources:
            return ""
        context_parts = []
        with self.lock:
            for full_key, entry in self.data.items():
                if entry["source"] in sources or "*" in sources:
                    context_parts.append(f"【{entry['source']}】{str(entry['value'])[:300]}")
        if context_parts:
            return "\n\n--- 其他Agent的发现 ---\n" + "\n".join(context_parts) + "\n---\n"
        return ""
    
    def clear(self):
        with self.lock:
            self.data.clear()


# ========== Agent ==========

class Agent:
    """Agent - 独立上下文 + 自定义模型"""
    
    def __init__(self, agent_id: str, config: dict, provider_config: dict, global_context_config: dict):
        self.agent_id = agent_id
        self.config = config
        self.provider_config = provider_config
        self.name = config.get("name", agent_id)
        self.icon = config.get("icon", "[Agent]")
        self.role = config.get("role", "")
        self.capabilities = config.get("capabilities", [])
        self.provider_id = config.get("provider", "")
        self.model = config.get("model", "")
        self.context = AgentContext(agent_id, config.get("context", {}), global_context_config)
        
        # 注入当前日期时间到系统提示
        import datetime
        now = datetime.datetime.now()
        current_time = now.strftime("%Y年%m月%d日 %H:%M:%S")
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
        
        base_prompt = config.get("prompt", "")
        date_aware_prompt = f"""【重要：当前时间】
今天是 {current_time} ({weekday})
请务必以这个时间为准，不要使用训练数据中的旧日期。

{base_prompt}"""
        
        self.context.set_system_prompt(date_aware_prompt)
        self.status = "idle"
        self.last_result = None
        self.total_tokens = 0
    
    def call_api(self, messages: List[dict], max_tokens: int = 2000, timeout: int = 120) -> dict:
        data = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens
        }).encode('utf-8')
        
        req = urllib.request.Request(
            self.provider_config.get("url", ""),
            data=data,
            headers={
                "Authorization": f"Bearer {self.provider_config.get('key', '')}",
                "Content-Type": "application/json"
            }
        )
        
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read().decode('utf-8'))
                # 处理GLM-5的reasoning_content字段
                msg = resp["choices"][0]["message"]
                content_text = msg.get("content") or msg.get("reasoning_content", "")
                
                return {
                    "ok": True,
                    "content": content_text,
                    "tokens": resp.get("usage", {}).get("total_tokens", 0)
                }
        except Exception as e:
            return {"ok": False, "error": str(e)}
    
    def chat(self, message: str, blackboard_context: str = "") -> dict:
        self.status = "running"
        enhanced_input = message + blackboard_context
        self.context.add_message("user", enhanced_input)
        
        result = self.call_api(self.context.get_messages())
        
        if result["ok"]:
            self.context.add_message("assistant", result["content"])
            self.total_tokens += result["tokens"]
            self.last_result = {
                "content": result["content"],
                "tokens": result["tokens"],
                "model": f"{self.provider_id}/{self.model}",
                "ctx_tokens": self.context.count_tokens(),
                "ctx_ratio": int(self.context.get_ratio() * 100)
            }
            self.status = "done"
        else:
            self.last_result = {
                "content": f"错误: {result['error']}",
                "tokens": 0,
                "model": f"{self.provider_id}/{self.model}",
                "ctx_tokens": self.context.count_tokens(),
                "ctx_ratio": int(self.context.get_ratio() * 100)
            }
            self.status = "error"
        
        return self.last_result
    
    def to_dict(self) -> dict:
        return {
            "id": self.agent_id,
            "name": self.name,
            "icon": self.icon,
            "role": self.role,
            "provider": self.provider_id,
            "model": self.model,
            "capabilities": self.capabilities,
            "status": self.status,
            "total_tokens": self.total_tokens,
            "ctx_tokens": self.context.count_tokens(),
            "ctx_ratio": int(self.context.get_ratio() * 100)
        }
    
    def reset_context(self):
        """重置上下文"""
        self.context.clear()
        self.status = "idle"
        self.last_result = None


# ========== 工作流引擎 ==========

class WorkflowEngine:
    """DAG工作流引擎"""
    
    def __init__(self, workflow_config: dict, agents: Dict[str, Agent]):
        self.workflow_config = workflow_config
        self.agents = agents
        self.steps = workflow_config.get("steps", [])
    
    def get_execution_order(self) -> List[List[str]]:
        depends_on = {}
        for step in self.steps:
            agent_id = step["agent"]
            deps = step.get("depends_on", [])
            depends_on[agent_id] = deps
        
        result = []
        completed = set()
        remaining = set(depends_on.keys())
        
        while remaining:
            ready = []
            for agent_id in remaining:
                if all(dep in completed for dep in depends_on.get(agent_id, [])):
                    ready.append(agent_id)
            
            if not ready:
                break
            
            result.append(ready)
            completed.update(ready)
            remaining -= set(ready)
        
        return result


# ========== 蜂群编排器 ==========

class SwarmOrchestrator:
    """蜂群编排器"""
    
    def __init__(self, config_path: str):
        self.config_loader = ConfigLoader(config_path)
        self.config = self.config_loader.config
        self.blackboard = Blackboard(self.config.get("communication", {}).get("blackboard", {}))
        self.web_search = WebSearch()  # 初始化联网搜索
        self.system_monitor = SystemMonitor()  # 初始化系统监控
        self.agents: Dict[str, Agent] = {}
        self._init_agents()
        self.state = {
            "status": "idle",
            "running": False,
            "current_workflow": None,
            "results": {},
            "tokens": 0,
            "log": [],
            "start_time": None,
            "rework_count": 0,
            "selected_agents": []
        }
    
    def _init_agents(self):
        providers = self.config_loader.get_providers()
        agents_config = self.config_loader.get_agents()
        context_config = self.config_loader.get_context_config()
        
        for agent_id, agent_config in agents_config.items():
            provider_id = agent_config.get("provider")
            provider_config = providers.get(provider_id, {})
            self.agents[agent_id] = Agent(agent_id, agent_config, provider_config, context_config)
        
        # 设置Blackboard订阅
        agent_ids = list(self.agents.keys())
        for agent_id in agent_ids:
            other_agents = [aid for aid in agent_ids if aid != agent_id]
            self.blackboard.subscribe(agent_id, other_agents)
    
    def log(self, msg: str, level: str = "info"):
        t = time.strftime("%H:%M:%S")
        prefix = "[!] " if level == "warning" else ""
        entry = f"[{t}] {prefix}{msg}"
        self.state["log"].append(entry)
        try:
            # Windows控制台兼容：替换emoji为ASCII
            safe_msg = msg
            safe_msg = safe_msg.replace('[CEO]', '[CEO]')
            safe_msg = safe_msg.replace('[Search]', '[Search]')
            safe_msg = safe_msg.replace('[Launch]', '[Launch]')
            safe_msg = safe_msg.replace('[Rework]', '[Rework]')
            safe_msg = safe_msg.replace('[Pass]', '[Pass]')
            safe_msg = safe_msg.replace('[Warning]', '[Warning]')
            safe_msg = safe_msg.replace('[Fail]', '[Fail]')
            safe_msg = safe_msg.replace('[Note]', '[Note]')
            safe_msg = safe_msg.replace('✓', '[OK]')
            safe_msg = safe_msg.replace('✗', '[X]')
            
            safe_entry = f"[{t}] {prefix}{safe_msg}"
            print(safe_entry)
        except Exception as e:
            # 如果还是失败，只打印ASCII字符
            try:
                print(f"[{t}] {entry.encode('ascii', errors='replace').decode('ascii')}")
            except:
                pass
    
    def get_agent(self, agent_id: str) -> Optional[Agent]:
        return self.agents.get(agent_id)
    
    def spawn_agent(self, agent_id: str, config: dict) -> Agent:
        providers = self.config_loader.get_providers()
        context_config = self.config_loader.get_context_config()
        provider_id = config.get("provider")
        provider_config = providers.get(provider_id, {})
        agent = Agent(agent_id, config, provider_config, context_config)
        self.agents[agent_id] = agent
        return agent
    
    def run_workflow(self, workflow_id: str, task: str, data: str = "") -> dict:
        workflow = self.config_loader.get_workflow(workflow_id)
        if not workflow:
            return {"error": f"工作流 '{workflow_id}' 不存在"}
        
        self.state["running"] = True
        self.state["current_workflow"] = workflow_id
        self.state["start_time"] = time.time()
        self.state["results"] = {}
        self.state["tokens"] = 0
        self.state["selected_agents"] = []
        
        self.log(f"[*] 开始执行工作流: {workflow.get('name', workflow_id)}")
        
        # 如果是智能调度模式，协调员先分析并决定搜索
        if workflow_id == "smart_dispatch":
            try:
                result = self._smart_dispatch(task, data)
                return result
            finally:
                self.state["running"] = False
                self.state["current_workflow"] = None
        
        try:
            engine = WorkflowEngine(workflow, self.agents)
            execution_order = engine.get_execution_order()
            self.log(f"执行顺序: {execution_order}")
            
            for batch in execution_order:
                if len(batch) == 1:
                    self._run_agent(batch[0], task, data)
                else:
                    threads = []
                    for agent_id in batch:
                        t = threading.Thread(target=self._run_agent, args=(agent_id, task, data))
                        t.start()
                        threads.append(t)
                    for t in threads:
                        t.join()
            
            output_agent = workflow.get("output", "")
            final_result = self.state["results"].get(output_agent, {})
            
            self.log(f"[+] 工作流完成，总tokens: {self.state['tokens']}")
            
            return {
                "ok": True,
                "workflow": workflow_id,
                "output": final_result,
                "all_results": self.state["results"],
                "tokens": self.state["tokens"],
                "duration": time.time() - self.state["start_time"]
            }
        
        except Exception as e:
            self.log(f"执行出错: {e}")
            traceback.print_exc()
            return {"error": str(e)}
        
        finally:
            self.state["running"] = False
    
    def _analyze_task_and_assign_roles(self, task: str) -> dict:
        """分析任务类型并分配角色"""
        task_lower = task.lower()
        
        # 任务类型关键词
        task_types = {
            "search": ["搜索", "查询", "查找", "调研", "信息", "数据收集"],
            "analysis": ["分析", "研究", "评估", "对比", "数据", "统计"],
            "code": ["代码", "开发", "编程", "API", "实现", "技术"],
            "writing": ["写作", "文章", "报告", "文档", "内容", "文案", "创作"],
            "stock": ["股票", "A股", "港股", "美股", "基金", "投资", "证券"]
        }
        
        # 检测任务类型
        detected_types = []
        for t_type, keywords in task_types.items():
            for kw in keywords:
                if kw in task_lower:
                    detected_types.append(t_type)
                    break
        
        # 股票代码检测
        if re.search(r'\d{6}\.(SH|SZ|BJ)', task):
            detected_types.append("stock")
        
        # Agent能力映射
        agent_capabilities = {
            "kimi": ["search", "analysis"],
            "glm": ["analysis", "code", "search"],
            "minimax": ["writing", "analysis"]
        }
        
        # 根据任务类型选择Agent
        selected_agents = []
        if not detected_types:
            selected_agents = ["kimi", "glm", "minimax"]
        else:
            for agent, caps in agent_capabilities.items():
                if any(t in caps for t in detected_types):
                    selected_agents.append(agent)
            if not selected_agents:
                selected_agents = ["kimi", "glm", "minimax"]
        
        # 生成角色分配
        role_prompts = {}
        for agent in selected_agents:
            if "stock" in detected_types:
                if agent == "kimi":
                    role_prompts[agent] = "负责股票数据搜索和行情信息收集"
                elif agent == "glm":
                    role_prompts[agent] = "负责技术面分析和基本面评估"
                elif agent == "minimax":
                    role_prompts[agent] = "负责整合分析报告"
            elif "search" in detected_types and agent == "kimi":
                role_prompts[agent] = "负责信息搜索和数据收集"
            elif "analysis" in detected_types and agent == "glm":
                role_prompts[agent] = "负责数据分析和逻辑推理"
            elif "writing" in detected_types and agent == "minimax":
                role_prompts[agent] = "负责内容整合和报告撰写"
            elif "code" in detected_types and agent == "glm":
                role_prompts[agent] = "负责技术实现和代码开发"
            else:
                role_prompts[agent] = "协作完成任务"
        
        return {
            "task_types": detected_types,
            "agents": selected_agents,
            "roles": role_prompts
        }

    def _smart_dispatch(self, task: str, data: str) -> dict:
        """智能调度模式 - CEO分析任务并分阶段执行"""
        self.log("[CEO] CEO开始分析任务...")

        # 1. CEO分析任务并分配
        coord_result = self.chat_with_agent("coordinator", f"请分析以下任务，并分阶段分配给团队成员:\n\n{task}")
        if not coord_result.get("content"):
            return {"error": "CEO规划失败"}

        coordination = coord_result["content"]
        self.state["results"]["coordinator"] = coord_result
        self.log("[CEO] CEO规划完成")

        # 2. 解析CEO的输出 - 支持新的 phases 格式
        phases = []
        try:
            import json
            json_match = re.search(r'```json\s*([\s\S]*?)\s*```', coordination)
            if not json_match:
                json_match = re.search(r'\{[\s\S]*"phases"[\s\S]*\}', coordination)
            
            if json_match:
                json_str = json_match.group(1) if '```' in json_match.group(0) else json_match.group(0)
                allocation = json.loads(json_str)
                phases = allocation.get("phases", [])
                self.log(f"[CEO] 解析到 {len(phases)} 个阶段")
            else:
                raise ValueError("未找到JSON格式")

        except Exception as e:
            self.log(f"[Warning] 解析失败: {e}")
            try:
                json_match = re.search(r'\{[\s\S]*"subtasks"[\s\S]*\}', coordination)
                if json_match:
                    allocation = json.loads(json_match.group(0))
                    subtasks = allocation.get("subtasks", [])
                    phase_tasks = {}
                    for st in subtasks:
                        agent = st.get("agent", "").lower()
                        task_content = st.get("task", "")
                        if agent and task_content:
                            phase_tasks[agent] = task_content
                    if phase_tasks:
                        phases = [{"phase": 1, "parallel": True, "agents": list(phase_tasks.keys()), "tasks": phase_tasks}]
            except:
                pass

        if not phases:
            role_assignment = self._analyze_task_and_assign_roles(task)
            agents = role_assignment["agents"]
            if agents:
                phase_tasks = {}
                for agent in agents:
                    phase_tasks[agent] = task
                phases = [{"phase": 1, "parallel": True, "agents": agents, "tasks": phase_tasks}]
                self.log(f"[CEO] 兜底分配: {agents}")

        # 3. 搜索
        needs_search = self._needs_search(task)
        search_context = ""
        if needs_search:
            self.log("[Search] 搜索信息...")
            search_result = self.web_search.search(task)
            if search_result.get("ok"):
                search_context = "\n--- 搜索结果 ---\n" + self.web_search.format_results(search_result)

        # 4. 按阶段执行
        selected_agents = []
        all_results = {}

        for phase_info in sorted(phases, key=lambda x: x.get("phase", 1)):
            phase_num = phase_info.get("phase", 1)
            parallel = phase_info.get("parallel", True)
            agents = phase_info.get("agents", [])
            tasks = phase_info.get("tasks", {})

            if not agents:
                continue

            self.log(f"[Phase {phase_num}] agents: {agents}, 并行: {parallel}")
            selected_agents.extend(agents)

            if parallel:
                threads = []
                for agent_id in agents:
                    agent_id = agent_id.lower()
                    agent_task = tasks.get(agent_id, task)
                    if search_context:
                        agent_task = f"{agent_task}\n\n{search_context}"
                    if all_results:
                        ctx = "\n--- 前面阶段结果 ---\n"
                        for a, r in all_results.items():
                            ctx += f"[{a}] {r[:500]}\n"
                        agent_task = f"{agent_task}\n{ctx}"
                    
                    t = threading.Thread(target=self._run_agent, args=(agent_id, agent_task, data))
                    t.start()
                    threads.append((agent_id, t))
                
                for agent_id, t in threads:
                    t.join()
                    if agent_id in self.state.get("results", {}):
                        all_results[agent_id] = self.state["results"][agent_id].get("content", "")
            else:
                for agent_id in agents:
                    agent_id = agent_id.lower()
                    agent_task = tasks.get(agent_id, task)
                    if search_context:
                        agent_task = f"{agent_task}\n\n{search_context}"
                    if all_results:
                        ctx = "\n--- 前面阶段结果 ---\n"
                        for a, r in all_results.items():
                            ctx += f"[{a}] {r[:500]}\n"
                        agent_task = f"{agent_task}\n{ctx}"
                    
                    self._run_agent(agent_id, agent_task, data)
                    if agent_id in self.state.get("results", {}):
                        all_results[agent_id] = self.state["results"][agent_id].get("content", "")

        # 5. 整合结果
        final_output = self._integrate_results()
        self.log(f"[Done] 完成，输出 {len(final_output)} 字符")

        self.state["selected_agents"] = list(set(selected_agents))

        return {
            "status": "completed",
            "output": final_output,
            "tokens": self.state["tokens"],
            "selected_agents": list(set(selected_agents))
        }

    def _quality_check(self, task: str) -> dict:
        """CEO质检审核 - 增强版"""
        # 收集所有结果
        findings = []
        total_length = 0
        has_real_data = False
        tool_calls = []
        
        for agent_id in ["kimi", "glm", "minimax"]:
            result = self.state["results"].get(agent_id, {})
            if result.get("content"):
                content = result["content"]
                total_length += len(content)
                
                agent = self.agents.get(agent_id)
                findings.append({
                    "agent": agent.name if agent else agent_id,
                    "role": agent.role if agent else "",
                    "content": content[:1000],
                    "length": len(content)
                })
                
                # 检查是否有真实数据
                import re
                # 检查股票代码
                if re.search(r'\d{6}\.(SH|SZ|BJ)', content):
                    has_real_data = True
                # 检查价格数据
                if re.search(r'\d+\.\d+.*[点|元|亿|万]', content):
                    has_real_data = True
                # 检查日期
                if re.search(r'20\d{2}年\d{1,2}月\d{1,2}日', content):
                    has_real_data = True
                # 检查数据表格
                if '|' in content and '---' in content:
                    has_real_data = True
        
        if not findings:
            return {"status": "fail", "reason": "无任何产出"}
        
        # 自动质量检查
        quality_issues = []
        
        # 1. 检查输出长度
        if total_length < 3000:
            quality_issues.append(f"输出总长度不足（{total_length}字，建议>3000字）")
        
        # 2. 检查是否有真实数据
        if not has_real_data:
            quality_issues.append("缺少真实数据（股票代码、价格、日期等）")
        
        # 3. 检查搜索是否执行
        if self._needs_search(task):
            # 检查是否有搜索结果标记
            search_performed = any(
                "搜索" in f.get("content", "") or "Search" in f.get("content", "")
                for f in findings
            )
            if not search_performed:
                quality_issues.append("未检测到联网搜索执行")
        
        # CEO审核
        qa_prompt = f"""作为CEO，请对以下开发成果进行质检审核：

【原始任务】
{task}

【开发成果统计】
- 总输出长度：{total_length}字
- 真实数据：{'有' if has_real_data else '无'}
- 参与Agent数：{len(findings)}

【自动检测结果】
{chr(10).join(['- ' + i for i in quality_issues]) if quality_issues else '- 自动检测通过'}

【各Agent产出】
"""
        for f in findings:
            qa_prompt += f"\n--- {f['agent']} ({f['role']}) [{f['length']}字] ---\n{f['content']}\n"
        
        qa_prompt += """

【质检标准】
1. 数据完整性：是否有真实数据支撑
2. 输出充分性：分析是否详尽
3. 逻辑清晰性：结论是否有依据
4. 可操作性：建议是否具体

【请输出】
[Pass] 质检结论：通过 / 返工
[Note] 评审意见：[具体说明]
[Tool] 返工要求：[如需返工，说明具体要求]
"""
        
        qa_result = self.chat_with_agent("coordinator", qa_prompt)
        
        if qa_result.get("content"):
            content = qa_result["content"]
            
            # 判断是否通过
            # 自动检测有严重问题 + CEO说返工 = 返工
            # 自动检测通过 + CEO说通过 = 通过
            # 其他情况看CEO意见
            
            has_critical_issues = len(quality_issues) >= 2
            ceo_says_rework = ("返工" in content or "[Rework]" in content or "[X]" in content or "重做" in content)
            ceo_says_pass = ("通过" in content or "[Pass]" in content or "[OK]" in content)
            
            self.state["results"]["quality_check"] = qa_result
            
            if has_critical_issues and ceo_says_rework:
                self.log("[CEO] CEO质检不通过，需要返工 ✗")
                return {"status": "rework", "review": content, "issues": quality_issues}
            elif ceo_says_pass and not has_critical_issues:
                self.log("[CEO] CEO质检通过 ✓")
                return {"status": "passed", "review": content}
            elif ceo_says_rework:
                self.log("[CEO] CEO质检不通过，需要返工 ✗")
                return {"status": "rework", "review": content, "issues": quality_issues}
            else:
                # 默认通过
                self.log("[CEO] CEO质检通过 ✓")
                return {"status": "passed", "review": content}
        
        return {"status": "unknown", "review": "审核失败"}
    
    def _qa_review(self, task: str) -> dict:
        """QA独立质检 - 不是CEO自检"""
        self.log("[QA] QA开始独立质检...")
        
        # 收集所有Agent的输出
        agent_outputs = {}
        for agent_id in ["kimi", "glm", "minimax"]:
            result = self.state["results"].get(agent_id, {})
            if result.get("content"):
                agent_outputs[agent_id] = {
                    "content": result["content"][:3000],  # 限制长度
                    "length": len(result["content"])
                }
        
        if not agent_outputs:
            return {
                "status": "fail",
                "score": 0,
                "summary": "没有任何Agent输出",
                "issues": [],
                "improvements": "请确保至少有一个Agent执行任务"
            }
        
        # 构建质检prompt
        qa_prompt = f"""请对以下Agent的输出进行独立质检：

## 原始任务
{task[:500]}

## Agent输出
"""
        for agent_id, output in agent_outputs.items():
            agent_name = self.agents.get(agent_id, {}).get("name", agent_id)
            qa_prompt += f"""
### {agent_name} (ID: {agent_id})
输出长度: {output['length']} 字符
输出内容:
{output['content'][:2000]}
"""
        
        qa_prompt += """

请按照质检标准进行评估，返回JSON格式的质检结果。"""

        # 调用QA Agent
        qa_result = self.chat_with_agent("qa", qa_prompt)
        
        if not qa_result.get("content"):
            # QA调用失败，使用规则检查
            return self._rule_based_check(task, agent_outputs)
        
        # 解析QA的JSON输出
        try:
            qa_content = qa_result["content"]
            # 提取JSON
            json_match = re.search(r'```json\s*([\s\S]*?)\s*```', qa_content)
            if not json_match:
                json_match = re.search(r'\{[\s\S]*"status"[\s\S]*\}', qa_content)
            
            if json_match:
                json_str = json_match.group(1) if '```' in json_match.group(0) else json_match.group(0)
                result = json.loads(json_str)
                
                self.log(f"[QA] 质检完成: {result.get('status')} (分数: {result.get('score', 'N/A')})")
                
                return {
                    "status": result.get("status", "rework"),
                    "score": result.get("score", 0),
                    "summary": result.get("summary", ""),
                    "issues": result.get("issues", []),
                    "improvements": result.get("improvements", "")
                }
        except Exception as e:
            self.log(f"[Warning] QA输出解析失败: {e}")
        
        # 解析失败，使用规则检查
        return self._rule_based_check(task, agent_outputs)
    
    def _rule_based_check(self, task: str, agent_outputs: dict) -> dict:
        """规则检查 - QA调用失败时的备选"""
        issues = []
        total_length = sum(o['length'] for o in agent_outputs.values())
        score = 60  # 基础分
        
        # 1. 检查总长度
        if total_length < 2000:
            issues.append({
                "agent": "all",
                "issue": f"输出总长度不足({total_length}字)，建议>2000字",
                "suggestion": "请各Agent增加输出深度"
            })
            score -= 20
        
        # 2. 检查是否有真实数据
        has_data = False
        for output in agent_outputs.values():
            text = output['content']
            # 股票代码、价格、日期、表格
            if re.search(r'\d{6}\.(SH|SZ|BJ)', text):
                has_data = True
            if re.search(r'\d+\.\d+', text):
                has_data = True
            if re.search(r'20\d{2}年', text):
                has_data = True
            if '|' in text and '---' in text:
                has_data = True
        
        if not has_data:
            issues.append({
                "agent": "all",
                "issue": "缺少具体数据支撑",
                "suggestion": "请搜索并引用真实数据"
            })
            score -= 15
        
        # 3. 检查是否有表格或列表
        has_structure = any(
            '|' in o['content'] or '1.' in o['content'] or '-' in o['content']
            for o in agent_outputs.values()
        )
        if not has_structure:
            issues.append({
                "agent": "all",
                "issue": "缺少结构化呈现",
                "suggestion": "请使用表格或列表组织内容"
            })
            score -= 10
        
        status = "passed" if score >= 70 else "rework"
        
        return {
            "status": status,
            "score": max(0, score),
            "summary": f"规则检查: {'通过' if status == 'passed' else '需要改进'}",
            "issues": issues,
            "improvements": "请根据上述问题改进输出质量"
        }


    def _summarize_results(self, task: str) -> str:
        """汇总所有Agent的结果"""
        findings = []
        for agent_id in ["kimi", "glm", "minimax"]:
            result = self.state["results"].get(agent_id, {})
            if result.get("content"):
                findings.append(f"【{self.agents[agent_id].name}】\n{result['content'][:500]}")
        
        if not findings:
            return "暂无结果"
        
        summary_prompt = f"请整合以下分析结果，生成简洁的汇总报告：\n\n任务：{task}\n\n" + "\n\n".join(findings)
        summary_result = self.chat_with_agent("minimax", summary_prompt)
        return summary_result.get("content", "汇总失败")
    
    def _run_agent(self, agent_id: str, task: str, data: str):
        agent = self.agents.get(agent_id)
        if not agent:
            self.log(f"Agent '{agent_id}' 不存在", "warning")
            return
        
        start_time = time.time()
        bb_context = self.blackboard.get_agent_context(agent_id)
        input_text = f"任务: {task}\n数据: {data}\n"
        
        # 检测任务类型并自动调用工具
        tool_results = self._auto_call_tools(task, agent_id)
        if tool_results:
            input_text += "\n--- 工具调用结果 ---\n"
            for tool_name, result in tool_results.items():
                if result.get("ok") or result.get("status") == "ok":
                    input_text += f"\n【{tool_name}】\n"
                    # 格式化工具结果
                    if isinstance(result, dict):
                        input_text += json.dumps(result, ensure_ascii=False, indent=2)[:2000]
                    else:
                        input_text += str(result)[:2000]
                    input_text += "\n"
            input_text += "\n---\n请基于以上工具数据进行分析。\n"
        
        # 所有Agent都支持联网搜索
        if self._needs_search(task):
            self.log(f"[{agent.icon}] 检测到需要联网搜索...")
            search_result = self.web_search.search(task)
            if search_result.get("ok"):
                search_context = "\n--- 联网搜索结果 ---\n"
                search_context += self.web_search.format_results(search_result)
                
                # 深度获取第一个网页
                results = search_result.get("results", [])
                if results and results[0].get("url"):
                    self.log(f"[{agent.icon}] 深度获取网页内容...")
                    fetch_result = self.web_search.fetch_url(results[0]["url"])
                    if fetch_result.get("ok"):
                        search_context += f"\n\n--- 详细内容 ---\n{fetch_result['content'][:5000]}"
                        self.log(f"[{agent.icon}] 已获取详细内容")
                
                search_context += "\n---\n请基于以上信息进行分析。\n"
                input_text += search_context
                self.log(f"[{agent.icon}] 已获取 {search_result.get('count', 0)} 条搜索结果")
        
        workflow = self.config_loader.get_workflow(self.state.get("current_workflow", ""))
        if workflow:
            for step in workflow.get("steps", []):
                if step.get("agent") == agent_id:
                    deps = step.get("depends_on", [])
                    if deps:
                        dep_results = []
                        for dep_id in deps:
                            dep_result = self.state["results"].get(dep_id, {})
                            if dep_result.get("content"):
                                # 记录Agent之间的交互
                                self.log(f"[{agent.icon}] {agent.name} ← {self.agents[dep_id].name} 获取分析结果")
                                dep_results.append(f"【{self.agents[dep_id].name}】{dep_result['content'][:500]}")
                        if dep_results:
                            input_text += "\n--- 前置分析 ---\n" + "\n".join(dep_results) + "\n---\n"
        
        self.log(f"[{agent.icon}] {agent.name} 开始执行...")
        result = agent.chat(input_text, bb_context)
        
        # 跟踪资源消耗
        duration = time.time() - start_time
        self.system_monitor.track_agent_request(
            agent_id, 
            result.get("tokens", 0), 
            duration
        )
        
        self.state["results"][agent_id] = result
        self.state["tokens"] += result.get("tokens", 0)
        
        if result.get("content"):
            self.blackboard.write(agent.name, "output", result["content"])
        
        ctx_status, ctx_ratio = agent.context.check_status()
        self.log(f"[{agent.icon}] {agent.name} 完成 ({result.get('tokens', 0)} tokens, 上下文: {int(ctx_ratio*100)}%)")
    
    def _auto_call_tools(self, task: str, agent_id: str) -> dict:
        """根据任务类型自动调用工具"""
        import re
        results = {}
        
        # 检测股票分析任务
        stock_pattern = r'(\d{6})\.(SH|SZ|BJ)|股票[代码]?[:：]?\s*(\d{6})|A股|大盘|板块'
        if re.search(stock_pattern, task):
            # 调用股票行情工具
            self.log(f"[{self.agents[agent_id].icon}] 自动调用股票分析工具...")
            
            # 尝试提取股票代码
            codes = []
            sh_match = re.findall(r'(\d{6})\.SH', task)
            sz_match = re.findall(r'(\d{6})\.SZ', task)
            codes.extend([c + '.SH' for c in sh_match])
            codes.extend([c + '.SZ' for c in sz_match])
            
            # 如果没有明确代码，使用默认大盘
            if not codes:
                codes = ['000001.SH', '399001.SZ']  # 上证指数、深证成指
            
            for code in codes[:3]:  # 最多3只
                try:
                    result = self._call_tool_proxy("stock_analysis", {"code": code})
                    if result.get("status") == "ok":
                        results[f"stock_analysis_{code}"] = result
                        self.log(f"[{self.agents[agent_id].icon}] 获取 {code} 行情成功")
                except Exception as e:
                    self.log(f"[{self.agents[agent_id].icon}] 获取 {code} 行情失败: {e}")
        
        # 检测数据可视化任务
        if any(kw in task for kw in ['图表', '可视化', '画图', '柱状图', '折线图', '饼图']):
            # 稍后由Agent调用generate_chart
            pass
        
        return results
    
    def _call_tool_proxy(self, tool_name: str, params: dict) -> dict:
        """调用工具代理"""
        try:
            import requests
            
            resp = requests.post(
                "http://localhost:8768/",
                json={"tool": tool_name, "params": params},
                timeout=30
            )
            return resp.json()
        except Exception as e:
            return {"error": str(e)}
    
    def _needs_search(self, text: str) -> bool:
        """判断是否需要联网搜索"""
        keywords = [
            "最新", "今天", "昨天", "近期", "最近", "当前", "目前",
            "实时", "新闻", "动态", "行情", "价格", "股价", "汇率",
            "天气", "温度", "现在", "本周", "本月", "今年",
            "怎么", "如何", "为什么", "是什么", "有哪些"
        ]
        return any(kw in text for kw in keywords)
    
    def chat_with_agent(self, agent_id: str, message: str) -> dict:
        agent = self.agents.get(agent_id)
        if not agent:
            return {"error": f"Agent '{agent_id}' 不存在"}
        
        bb_context = self.blackboard.get_agent_context(agent_id)
        input_text = message
        
        # 所有Agent都支持联网搜索 + 深度获取
        if self._needs_search(message):
            self.log(f"[{agent.icon}] 检测到需要联网搜索...")
            search_result = self.web_search.search(message)
            if search_result.get("ok"):
                search_context = "\n--- 联网搜索结果 ---\n"
                search_context += self.web_search.format_results(search_result)
                
                # 深度搜索：获取第一个网页的详细内容
                results = search_result.get("results", [])
                if results and results[0].get("url"):
                    self.log(f"[{agent.icon}] 深度获取网页内容...")
                    fetch_result = self.web_search.fetch_url(results[0]["url"])
                    if fetch_result.get("ok"):
                        search_context += f"\n\n--- 详细内容 ---\n{fetch_result['content'][:5000]}"
                        self.log(f"[{agent.icon}] 已获取详细内容")
                
                search_context += "\n---\n请基于以上信息回答用户问题。\n"
                input_text += search_context
                self.log(f"[{agent.icon}] 已获取 {search_result.get('count', 0)} 条搜索结果")
        
        result = agent.chat(input_text, bb_context)
        if result.get("content"):
            self.blackboard.write(agent.name, "chat", result["content"])
        return result
    
    def get_status(self) -> dict:
        return {
            **self.state,
            "agents": {aid: agent.to_dict() for aid, agent in self.agents.items()},
            "blackboard": self.blackboard.read(),
            "workflows": list(self.config_loader.get_workflows().keys()),
            "system": self.system_monitor.get_system_stats(),
            "agent_stats": self.system_monitor.get_agent_stats()
        }
    
    def reset(self):
        for agent in self.agents.values():
            agent.context.clear()
            agent.status = "idle"
            agent.last_result = None
        self.blackboard.clear()
        self.system_monitor.reset()  # 重置系统监控统计
        self.state = {
            "status": "idle",
            "running": False,
            "current_workflow": None,
            "results": {},
            "tokens": 0,
            "log": [],
            "start_time": None,
        }
        self.log("[+] 系统已重置")


# ========== Web服务器 ==========

class Handler(BaseHTTPRequestHandler):
    orchestrator: SwarmOrchestrator = None
    
    def log_message(self, *args):
        pass
    
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self.send_html(INDEX_HTML)
        elif self.path == '/monitor' or self.path == '/monitor.html':
            self.send_html(MONITOR_HTML)
        elif self.path == '/api/status':
            self.send_json(self.orchestrator.get_status())
        elif self.path == '/api/agents':
            self.send_json({aid: a.to_dict() for aid, a in self.orchestrator.agents.items()})
        elif self.path == '/api/workflows':
            self.send_json(self.orchestrator.config_loader.get_workflows())
        elif self.path == '/api/blackboard':
            self.send_json(self.orchestrator.blackboard.read())
        elif self.path.startswith('/api/agent/') and self.path.endswith('/history'):
            # 获取单个Agent的对话历史
            agent_id = self.path.split('/')[3]
            agent = self.orchestrator.agents.get(agent_id)
            if agent:
                self.send_json({"messages": agent.context.get_messages()})
            else:
                self.send_json({"error": "Agent not found"}, 404)
        else:
            self.send_error(404)
    
    def do_POST(self):
        if self.path == '/api/run':
            self._handle_run()
        elif self.path == '/api/chat':
            self._handle_chat()
        elif self.path == '/api/search':
            self._handle_search()
        elif self.path == '/api/fetch':
            self._handle_fetch()
        elif self.path == '/api/summarize':
            self._handle_summarize()
        elif self.path == '/api/reset':
            self.orchestrator.reset()
            self.send_json({"ok": True})
        else:
            self.send_error(404)
    
    def _handle_search(self):
        """联网搜索"""
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            query = data.get('query', '')
            max_results = data.get('max_results', 5)
            
            if not query:
                self.send_json({"error": "Missing query"}, 400)
                return
            
            result = self.orchestrator.web_search.search(query, max_results)
            self.send_json(result)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
    
    def _handle_fetch(self):
        """获取网页内容 - 使用MCP fetch服务"""
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            url = data.get('url', '')
            
            if not url:
                self.send_json({"error": "Missing url"}, 400)
                return
            
            result = self.orchestrator.web_search.fetch_url(url)
            self.send_json(result)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
    
    def _handle_run(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            workflow = data.get('workflow', 'pipeline')
            task = data.get('task', '')
            extra_data = data.get('data', '')
            threading.Thread(
                target=self.orchestrator.run_workflow,
                args=(workflow, task, extra_data),
                daemon=True
            ).start()
            self.send_json({"ok": True, "workflow": workflow})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
    
    def _handle_chat(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            agent_id = data.get('agent')
            message = data.get('message')
            if not agent_id or not message:
                self.send_json({"error": "Missing agent or message"}, 400)
                return
            result = self.orchestrator.chat_with_agent(agent_id, message)
            self.send_json(result)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
    
    def _handle_summarize(self):
        """汇总所有Agent的发现"""
        try:
            # 收集所有Agent的最新发现
            findings = []
            for agent_id, agent in self.orchestrator.agents.items():
                if agent.last_result and agent.last_result.get("content"):
                    findings.append({
                        "agent": agent.name,
                        "icon": agent.icon,
                        "content": agent.last_result["content"]
                    })
            
            if not findings:
                self.send_json({"error": "暂无发现可汇总"})
                return
            
            # 使用撰写员汇总
            writer = self.orchestrator.agents.get("writer")
            if not writer:
                # 如果没有撰写员，直接拼接
                summary = "# 汇总报告\n\n"
                for f in findings:
                    summary += f"## {f['icon']} {f['agent']}\n\n{f['content']}\n\n---\n\n"
                self.send_json({"summary": summary})
                return
            
            # 构建汇总请求
            summary_prompt = "请汇总以下各Agent的发现，生成一份简洁的综合报告：\n\n"
            for f in findings:
                summary_prompt += f"【{f['icon']} {f['agent']}】\n{f['content'][:1000]}\n\n"
            summary_prompt += "\n请生成一份结构清晰的汇总报告，包含：核心发现、关键洞察、建议行动。"
            
            result = self.orchestrator.chat_with_agent("writer", summary_prompt)
            
            if result.get("content"):
                self.send_json({"summary": result["content"]})
            else:
                self.send_json({"error": result.get("error", "汇总失败")})
                
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
    
    def send_html(self, content: str, status: int = 200):
        b = content.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(b))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(b)
    
    def send_json(self, data: dict, status: int = 200):
        b = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(b))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(b)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# 前端页面（从文件读取）
INDEX_HTML = ""
MONITOR_HTML = ""


def start_tool_proxy():
    """自动启动工具代理服务"""
    import subprocess
    import sys
    
    tool_proxy_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tool_proxy.py')
    
    if os.path.exists(tool_proxy_path):
        try:
            # 检查端口是否已被占用
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('localhost', 8768))
            sock.close()
            
            if result != 0:  # 端口未被占用，启动服务
                print("[*] 启动工具代理服务...")
                process = subprocess.Popen(
                    [sys.executable, tool_proxy_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                import time
                time.sleep(1)  # 等待服务启动
                print("[+] 工具代理服务已启动 (PID: {})".format(process.pid))
            else:
                print("[*] 工具代理服务已在运行")
        except Exception as e:
            print(f"[!] 启动工具代理失败: {e}")
    else:
        print(f"[!] 工具代理脚本不存在: {tool_proxy_path}")


def main():
    import webbrowser
    
    global INDEX_HTML
    
    # 自动启动工具代理
    start_tool_proxy()
    
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    orchestrator = SwarmOrchestrator(config_path)
    Handler.orchestrator = orchestrator
    
    index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            INDEX_HTML = f.read()
    except:
        INDEX_HTML = "<h1>前端页面加载失败，请检查 index.html</h1>"
    
    # 加载监控台页面
    monitor_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'monitor.html')
    global MONITOR_HTML
    try:
        with open(monitor_path, 'r', encoding='utf-8') as f:
            MONITOR_HTML = f.read()
    except:
        MONITOR_HTML = "<h1>监控台页面加载失败，请检查 monitor.html</h1>"
    
    print('='*60)
    print('蜂群AGS V4 - 独立对话 + 智能汇总')
    print('='*60)
    print(f'配置文件: {config_path}')
    print(f'访问地址: http://localhost:8767')
    print(f'监控台: http://localhost:8767/monitor')
    print('='*60)

    webbrowser.open('http://localhost:8767')

    server = ThreadedHTTPServer(('localhost', 8767), Handler)
    server.serve_forever()


if __name__ == '__main__':
    main()




