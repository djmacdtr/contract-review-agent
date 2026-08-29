# GLM 响应模式与第一档扩容 Canary 记录

日期：2026-08-28

## 三模式最小探针

同一份合成数值输入分别调用 `prompt_only`、`json_object`、`json_schema` 各一次。三种模式均为 HTTP 200、`finish_reason=stop`、实际模型 `GLM-5.3-Flash`，每次均返回 1 个候选决策并通过 Pydantic 与全集校验。

探针关闭 HTTP 重试，安全输出保存于：`.real-diagnostic-temp/glm-numeric-response-mode-probe-20260828.json`。

## 第一档扩容

- 输出上限：8192 tokens。
- payload 上限：24000 字符。
- numeric 结构单元上限：12。
- text 结构单元上限：24。
- Canary 并发：1。
- HTTP 重试：关闭，仅用于 Canary 隔离。

## 双 Canary

### Numeric

- 结果：成功。
- 批次：`batch_4bb1099616fc5aede8158f64`。
- 结构单元：12。
- 候选：16；全部分类通过。
- HTTP：200；`finish_reason=stop`。

### Text

- 结果：失败。
- 批次：`batch_296c08a0a2ff640617dffc3a`。
- 结构单元：24。
- HTTP：200；`finish_reason=length`。
- 安全错误：`LLM_OUTPUT_TRUNCATED`。

两次 Canary 共 2 次 LLM 请求，未创建正式任务。安全输出保存于：`.real-diagnostic-temp/expanded-fact-canary-20260828.json`。

## 结论

三种响应模式在最小输入上均可用，不能据此判定 `json_schema` 独有故障。第一档 numeric 扩容通过，但 text 24 单元在 8192 tokens 下仍发生截断；按止损规则本轮不创建正式任务、不继续调大参数、不改变 batch 兼容逻辑。Docker API、PostgreSQL、Worker 均已保持运行，未执行 commit、push、reset、clean，也未清理 `.real-diagnostic-temp/`。
