import type { PassedCheck } from '../src/api/types.ts'
import { formatPassedCheckDescription } from '../src/utils/passedCheckDescription.ts'

function assertEqual(actual: unknown, expected: unknown) {
  if (actual !== expected) throw new Error(`expected ${String(expected)}, got ${String(actual)}`)
}

function check(module_code: string, title: string, description: string): PassedCheck {
  return { check_id: `check_${module_code}`, module_code, title, description }
}

assertEqual(
  formatPassedCheckDescription(check('TEMPLATE_COMPLETENESS', '未发现明确漏填标记', '已执行占位符、空白线和基础表格必填检查。')),
  '经 AI 对合同中的占位符、空白线及基础表格必填区域进行智能核验，当前未发现明确漏填或异常缺失。',
)
assertEqual(
  formatPassedCheckDescription(check('FACT_CONSISTENCY', '租赁期间来源一致', '至少两个来源的规范化事实值一致。')),
  '经 AI 结合合同正文与辅助资料对租赁期间进行交叉核验，相关信息在当前可用证据范围内保持一致，未发现冲突。',
)
assertEqual(
  formatPassedCheckDescription(check('FACT_CONSISTENCY', '首期利率来源一致', '至少两个来源的规范化事实值一致。')),
  '经 AI 对合同及相关资料中的首期利率信息进行关联分析，已识别数据保持一致，未发现数值差异。',
)
assertEqual(
  formatPassedCheckDescription(check('FACT_CONSISTENCY', '租金期数来源一致', '至少两个来源的规范化事实值一致。')),
  '经 AI 对多份资料中的租金期数进行智能比对，相关约定保持一致，未发现异常变更。',
)

const lease = formatPassedCheckDescription(check('FACT_CONSISTENCY', '租赁期间来源一致', '至少两个来源的规范化事实值一致。'))
const amount = formatPassedCheckDescription(check('FACT_CONSISTENCY', '租金金额来源一致', '至少两个来源的规范化事实值一致。'))
assertEqual(lease === amount, false)

assertEqual(
  formatPassedCheckDescription(check('NUMERIC_CONSISTENCY', '月租金与总租金关系', '声明式数值规则通过。')),
  '经 AI 对合同及相关资料中的月租金与总租金关系进行关联分析，相关数值关系保持一致，未发现数值差异。',
)
assertEqual(
  formatPassedCheckDescription(check('TEMPLATE_INTEGRITY', '日期未发生变化', '本次文档实际包含日期内容，已完成对应比较且未发现差异。')),
  '经 AI 对合同及相关资料中的日期信息进行关联核验，当前未发现矛盾、遗漏或异常变更。',
)

assertEqual(
  formatPassedCheckDescription(check('FACT_CONSISTENCY', '付款方式来源一致', '经 AI 已结合正文与资料完成交叉核验，未发现冲突。')),
  '经 AI 已结合正文与资料完成交叉核验，未发现冲突。',
)

const technicalText = formatPassedCheckDescription(check('NUMERIC_CONSISTENCY', '金额关系', '声明式数值规则通过。'))
if (/模型|任务 ID|check_|声明式|validation/u.test(technicalText)) {
  throw new Error(`technical text leaked into display copy: ${technicalText}`)
}
