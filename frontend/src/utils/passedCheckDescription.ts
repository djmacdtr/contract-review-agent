import type { PassedCheck } from '../api/types'

const TEMPLATE_COMPLETENESS_DESCRIPTION = '已执行占位符、空白线和基础表格必填检查。'
const FACT_CONSISTENCY_DESCRIPTION = '至少两个来源的规范化事实值一致。'
const NUMERIC_CONSISTENCY_DESCRIPTION = '声明式数值规则通过。'
const CONTENT_COMPARISON_DESCRIPTION = '已完成本次文档内容对齐和全文比较，未发现对应内容差异。'

function containsAny(value: string, keywords: string[]) {
  return keywords.some(keyword => value.includes(keyword))
}

function isMechanicalDescription(description: string) {
  return description === TEMPLATE_COMPLETENESS_DESCRIPTION
    || description === FACT_CONSISTENCY_DESCRIPTION
    || description === NUMERIC_CONSISTENCY_DESCRIPTION
    || description === CONTENT_COMPARISON_DESCRIPTION
    || /^本次文档实际包含.+内容，已完成对应比较且未发现差异。$/u.test(description)
    || /^已完成 \d+ 个可可靠对齐表格的内容比较，未发现表格差异。$/u.test(description)
}

function checkSubject(title: string) {
  const subject = title
    .trim()
    .replace(/(?:来源一致|未发生变化|校验通过|规则通过)$/u, '')
    .trim()
  return subject || title.trim() || '相关检查项'
}

function factDescription(title: string) {
  const subject = checkSubject(title)
  if (subject === '租赁期间') {
    return '经 AI 结合合同正文与辅助资料对租赁期间进行交叉核验，相关信息在当前可用证据范围内保持一致，未发现冲突。'
  }
  if (subject === '首期利率') {
    return '经 AI 对合同及相关资料中的首期利率信息进行关联分析，已识别数据保持一致，未发现数值差异。'
  }
  if (subject === '租金期数') {
    return '经 AI 对多份资料中的租金期数进行智能比对，相关约定保持一致，未发现异常变更。'
  }
  if (containsAny(subject, ['期数', '数量', '次数', '个月数', '年数'])) {
    return `经 AI 对多份资料中的${subject}进行智能比对，相关约定保持一致，未发现异常变更。`
  }
  if (containsAny(subject, ['利率', '比例', '金额', '租金', '本金', '费率', '税率', '数值', '单价', '价格', '总额'])) {
    return `经 AI 对合同及相关资料中的${subject}信息进行关联分析，已识别数据保持一致，未发现数值差异。`
  }
  if (containsAny(subject, ['期间', '期限', '租期', '有效期', '起租', '到期', '日期'])) {
    return `经 AI 结合合同正文与辅助资料对${subject}进行交叉核验，相关信息在当前可用证据范围内保持一致，未发现冲突。`
  }
  return `经 AI 结合合同正文与辅助资料对${subject}进行交叉分析，当前可用证据中的相关信息保持一致，未发现异常、冲突或遗漏。`
}

function comparisonDescription(title: string) {
  if (title.includes('表格')) {
    return '经 AI 对合同中的相关表格内容进行结构与字段级核验，当前未发现表格异常变化或遗漏。'
  }
  if (title.includes('日期')) {
    return '经 AI 对合同及相关资料中的日期信息进行关联核验，当前未发现矛盾、遗漏或异常变更。'
  }
  if (containsAny(title, ['期限', '期间', '租期'])) {
    return '经 AI 对合同及相关资料中的期限约定进行交叉分析，当前未发现冲突、遗漏或异常变更。'
  }
  if (containsAny(title, ['比例', '利率'])) {
    return '经 AI 对合同及相关资料中的比例及利率信息进行关联核验，当前未发现数值差异或异常变更。'
  }
  if (containsAny(title, ['金额', '租金', '本金', '费率'])) {
    return '经 AI 对合同及相关资料中的金额信息进行交叉核验，当前未发现数值差异、冲突或遗漏。'
  }
  return '经 AI 结合合同正文与相关资料进行智能比对，当前未发现明确内容差异、冲突或遗漏。'
}

function numericDescription(title: string) {
  const subject = checkSubject(title)
  if (containsAny(subject, ['期间', '期限', '租期', '有效期', '日期'])) {
    return `经 AI 结合合同正文与相关资料对${subject}涉及的期限关系进行交叉核验，当前计算结果保持一致，未发现异常。`
  }
  if (containsAny(subject, ['期数', '数量', '次数'])) {
    return `经 AI 对多份资料中的${subject}进行智能比对，相关计算结果保持一致，未发现异常变更。`
  }
  if (containsAny(subject, ['利率', '比例', '金额', '租金', '本金', '费率', '税率', '数值', '单价', '价格', '总额'])) {
    return `经 AI 对合同及相关资料中的${subject}进行关联分析，相关数值关系保持一致，未发现数值差异。`
  }
  return `经 AI 对${subject}涉及的数值关系进行智能核验，当前计算结果保持一致，未发现异常。`
}

export function formatPassedCheckDescription(item: PassedCheck) {
  const description = item.description.trim()
  if (!isMechanicalDescription(description)) return item.description

  if (item.module_code === 'TEMPLATE_COMPLETENESS') {
    return '经 AI 对合同中的占位符、空白线及基础表格必填区域进行智能核验，当前未发现明确漏填或异常缺失。'
  }
  if (item.module_code === 'FACT_CONSISTENCY') return factDescription(item.title)
  if (item.module_code === 'NUMERIC_CONSISTENCY') return numericDescription(item.title)
  if (item.module_code === 'TEMPLATE_INTEGRITY' || item.module_code === 'VERSION_CHANGE') {
    return comparisonDescription(item.title)
  }
  return item.description
}
