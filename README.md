# 账单系统

基于 new-api 多站点架构的成本对账与利润核算系统。重构版，单一服务、内置网页，一步步构建。

> 需求：见 [`docs/账单系统需求规格.md`](docs/账单系统需求规格.md)

## 当前进度
- **第 1 步（已完成）**：配置总站只读连接，网页查看某天各渠道·各模型的消耗（消耗量 quota / 调用次数）。

## 本地运行（最简，SQLite）
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 打开 http://localhost:8000
```
自有数据默认存到 `./data/bill.db`（SQLite），零配置。

## Docker 部署（PostgreSQL，端口可自定义）
```bash
cp .env.example .env   # 改 APP_PORT、POSTGRES_PASSWORD
docker compose up -d --build
# 打开 http://服务器IP:APP_PORT
```

## 使用
1. 「站点」页新增总站，填只读 DSN（如 `mysql+pymysql://用户:密码@主机:3306/库名`），点「测试连接」确认通；
2. 「渠道消耗」页选站点+日期，查看当天各渠道消耗。
