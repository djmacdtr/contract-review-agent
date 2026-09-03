# 甲方 OCR 服务与传输方式诊断

- 时间：2026-09-02 09:46:59 +08:00
- 状态：DIAGNOSED
- 当前提交：`55d356d`

## 诊断结论

- 甲方 OCR 服务可用：唯一真实探针返回 HTTP 200、业务码 200。
- 测试文件大小 2,061,265 bytes；8/8 页解析完成；耗时约 26 秒。
- 返回了可定位的印章影像节点，未记录正文、响应原文、外部地址或凭据。
- 失败任务 `tsk_01M1FWFQBR44K44E1H0EYN2P3R` 的根错误为 `OCR_SERVICE_UNAVAILABLE / NETWORK_ERROR`，三次请求约 2.2 秒内终止，未获得 HTTP 状态或解析结构。

## 根因定位

- 成功探针以普通文件流发送，并带有明确 `Content-Length`。
- 生产异步客户端使用异步生成器作为请求体，HTTPX 自动生成 `Transfer-Encoding: chunked`。
- DNS 和 TCP 端口均正常；结合服务探针成功与正式请求快速断连，当前证据指向甲方 OCR 网关不兼容 chunked 请求体。

## 下一步

- 在生产 OCR 客户端保持 1MB 流式读取，同时显式发送经过本地文件大小校验的 `Content-Length`，避免整文件加载内存。
- 修改后只执行一次小 DOCX Canary；成功后再创建新的控制台任务，不 retry 已失败任务。
