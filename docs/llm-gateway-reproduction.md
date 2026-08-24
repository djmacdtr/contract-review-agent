# LLM 网关调用与复现说明

## 1. 本次实际调用参数

测试时间：2026-08-24，宿主机环境，未经过 Docker。

网关基础地址：

```text
http://10.50.11.18:8080/v1
```

公共请求头：

```http
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

请求超时为 30 秒。请求中未携带合同内容、`temperature`、`response_format` 或其他扩展参数。

### 1.1 获取模型列表

```http
GET /v1/models HTTP/1.1
Host: 10.50.11.18:8080
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

本机实际结果：HTTP 502。

### 1.2 使用 GLM-5.2 对话

```http
POST /v1/chat/completions HTTP/1.1
Host: 10.50.11.18:8080
Authorization: Bearer <API_KEY>
Content-Type: application/json

{
  "model": "GLM-5.2",
  "messages": [
    {
      "role": "user",
      "content": "仅回复 OK"
    }
  ],
  "stream": false,
  "max_tokens": 16
}
```

本机实际结果：HTTP 502。

### 1.3 使用 `text` 场景别名对话

除 `model` 外，其他参数完全相同：

```json
{
  "model": "text",
  "messages": [
    {
      "role": "user",
      "content": "仅回复 OK"
    }
  ],
  "stream": false,
  "max_tokens": 16
}
```

本机实际结果：HTTP 502。

## 2. Python 完整复现脚本

安装依赖：

```powershell
python -m pip install httpx
```

先在当前 PowerShell 会话设置 API Key：

```powershell
$secureLlmKey = Read-Host "LLM API Key" -AsSecureString
$llmCredential = [PSCredential]::new("unused", $secureLlmKey)
$env:LLM_API_KEY = $llmCredential.GetNetworkCredential().Password
```

保存以下内容为 `probe_llm.py`：

```python
import asyncio
import os

import httpx


BASE_URL = "http://10.50.11.18:8080/v1"
API_KEY = os.environ["LLM_API_KEY"]


async def main() -> None:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    checks = [
        ("models", "GET", f"{BASE_URL}/models", None),
        (
            "glm_chat",
            "POST",
            f"{BASE_URL}/chat/completions",
            {
                "model": "GLM-5.2",
                "messages": [{"role": "user", "content": "仅回复 OK"}],
                "stream": False,
                "max_tokens": 16,
            },
        ),
        (
            "text_alias_chat",
            "POST",
            f"{BASE_URL}/chat/completions",
            {
                "model": "text",
                "messages": [{"role": "user", "content": "仅回复 OK"}],
                "stream": False,
                "max_tokens": 16,
            },
        ),
    ]

    # trust_env=True 与项目客户端默认行为一致。
    async with httpx.AsyncClient(timeout=30.0, trust_env=True) as client:
        for stage, method, url, payload in checks:
            try:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=payload,
                )
                print(
                    {
                        "stage": stage,
                        "status_code": response.status_code,
                        "content_type": (
                            response.headers.get("content-type") or ""
                        ).split(";", 1)[0],
                    }
                )
                # 需要提交网关错误详情时可临时取消下一行注释；
                # 检查内容中没有凭据或内部敏感信息后再转发。
                # print(response.text[:2000])
            except httpx.RequestError as exc:
                print(
                    {
                        "stage": stage,
                        "network_error": type(exc).__name__,
                    }
                )


asyncio.run(main())
```

运行：

```powershell
python .\probe_llm.py
```

本机核心观测结果：

```text
models: 502
glm_chat: 502
text_alias_chat: 502
```

脚本还会输出实际响应的 `content_type`；定位故障主要看 `status_code`。

## 3. curl 复现

PowerShell 中请显式使用 `curl.exe`，避免 Windows PowerShell 把 `curl` 解析成其他命令。

模型列表：

```powershell
curl.exe --max-time 30 -i `
  -H "Authorization: Bearer $env:LLM_API_KEY" `
  -H "Content-Type: application/json" `
  "http://10.50.11.18:8080/v1/models"
```

GLM-5.2 对话：

```powershell
curl.exe --max-time 30 -i `
  -H "Authorization: Bearer $env:LLM_API_KEY" `
  -H "Content-Type: application/json" `
  --data-raw '{"model":"GLM-5.2","messages":[{"role":"user","content":"仅回复 OK"}],"stream":false,"max_tokens":16}' `
  "http://10.50.11.18:8080/v1/chat/completions"
```

如 PowerShell/curl 对中文编码处理异常，可把测试内容临时改成 `Reply only OK`；这不会改变连通性诊断结论。

## 4. 可选：绕过环境配置进行对照

把 Python 脚本中的：

```python
httpx.AsyncClient(timeout=30.0, trust_env=True)
```

改为：

```python
httpx.AsyncClient(timeout=30.0, trust_env=False)
```

本机对照结果为：

- `/v1/models`：`RemoteProtocolError`
- `GLM-5.2` completion：`ReadError`
- `text` completion：`ReadError`

这表示该路径下连接在取得完整 HTTP 响应前被关闭。正常项目调用使用默认的 `trust_env=True`，其稳定返回 HTTP 502。

## 5. 状态码判断

依据《集团内部 LLM 网关使用指南》：

| 状态码 | 含义 |
|---|---|
| 401 | API Key 缺失或无效 |
| 403 | 应用被禁用或已过期 |
| 404 | 模型不存在、未配置或接口路径错误 |
| 429 | 请求过于频繁 |
| 500 | 网关内部错误 |
| 502 | 后端模型服务不可用 |

本次合法模型 `GLM-5.2`、场景别名 `text` 和模型列表接口均返回 502，因此应优先由 AI 平台团队检查网关反向代理、模型后端注册、后端实例健康状态和零信任网络权限。

## 6. 项目配置注意事项

项目当前配置为：

```text
LLM_EXTRACTION_MODEL=GLM-5.2
LLM_ADVICE_MODEL=GLM-5.2
```

