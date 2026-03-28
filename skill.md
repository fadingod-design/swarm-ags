# 蜂群AGS V4.5.2

多Agent协作系统，支持智能调度、阶段执行、联网搜索等功能。

## 启动

```bash
python swarm.py
```

## 访问

- 控制台: http://localhost:8767/
- 监控台: http://localhost:8767/monitor.html

## 工作流

- smart_dispatch: 智能调度（推荐）
- pipeline: 流水线
- parallel: 并行研究
- quick: 快速研究

## V4.5.2 更新 (2026-03-28)

### 阶段执行系统
- **phases**: CEO 可指定分阶段执行，支持串行/并行控制
- **最小分配**: 简单任务可以只分配给 1 个 Agent
- **依赖传递**: 后阶段能看到前阶段的结果

### 可视化界面
- **六边形节点**: CEO(六边形)、Kimi(五边形)、GLM(四边形)、MiniMax(八边形)
- **粒子连线**: Agent 交互时自动绘制带粒子的渐变连线
- **呼吸动画**: 运行中的 Agent 有脉冲动画效果
- **消息流**: 实时显示 Agent 间的消息传递

### 功能改进
- **联网搜索**: 自动判断是否需要搜索，支持 EnhancedSearch
- **删除 QA**: 简化工作流，不再需要单独的质检 Agent
- **智能分配**: CEO 根据任务复杂度决定分配给多少 Agent

## 配置

环境变量 .env:
```
QIANFAN_API_KEY=xxx
INFINI_API_KEY=xxx
AUTOGLM_APP_ID=100003
AUTOGLM_APP_KEY=xxx
```

## Agents

| Agent | 能力 | 模型 |
|-------|------|------|
| CEO | 决策、规划、调度 | glm-5 |
| Kimi | 联网搜索、信息收集 | kimi-k2.5 |
| GLM | 数据分析、代码开发 | glm-5 |
| MiniMax | 内容创作、报告撰写 | minimax-m2.7 |
