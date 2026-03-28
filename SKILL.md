# 蜂群AGS V4.5.2

多Agent协作系统，支持智能调度、返工重做、联网搜索等功能。

## 启动

```bash
python swarm.py
```

## 访问

- 工作台: http://localhost:8767/ （推荐）
- 监控台: http://localhost:8767/monitor.html

## 工作流

- smart_dispatch: 智能调度（推荐）
- pipeline: 流水线
- parallel: 并行研究
- quick: 快速研究

## V4.5.2 更新 (2026-03-25)

### 网状可视化
- **多边形节点**: CEO(六边形)、Kimi(五边形)、GLM(四边形)、MiniMax(八边形)
- **交互连线**: Agent状态变化时自动生成连线动画
- **渐变色**: 每个Agent使用专属颜色
- **节点动画**: 只在Agent工作时才有呼吸发光效果

### 连线逻辑
- CEO → Agent: 任务分配
- Agent → CEO: 任务完成
- Kimi/GLM → MiniMax: 结果整合

### 重要修复
- **Kimi API**: 改用百度千帆API
- **GLM-5格式**: 支持reasoning_content
- **EnhancedSearch**: 多搜索引擎

## 配置

创建 .env:
```
QIANFAN_API_KEY=xxx
INFINI_API_KEY=xxx
AUTOGLM_APP_ID=100003
AUTOGLM_APP_KEY=xxx
```

## GitHub

https://github.com/fadingod-design/swarm-ags
