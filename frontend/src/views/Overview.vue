<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "采集源登记", "created": 0, "pending": 0, "abnormal": 0}, {"name": "解析规则", "created": 0, "pending": 0, "abnormal": 0}, {"name": "代理调度", "created": 0, "pending": 0, "abnormal": 0}, {"name": "限速策略", "created": 0, "pending": 0, "abnormal": 0}, {"name": "采集任务", "created": 0, "pending": 0, "abnormal": 0}, {"name": "预处理管道", "created": 0, "pending": 0, "abnormal": 0}, {"name": "RPA流程编排", "created": 0, "pending": 0, "abnormal": 0}, {"name": "失败重试", "created": 0, "pending": 0, "abnormal": 0}, {"name": "结果校验", "created": 0, "pending": 0, "abnormal": 0}, {"name": "采集告警", "created": 0, "pending": 0, "abnormal": 0}, {"name": "诊断记录", "created": 0, "pending": 0, "abnormal": 0}, {"name": "调度计划", "created": 0, "pending": 0, "abnormal": 0}, {"name": "输出配置", "created": 0, "pending": 0, "abnormal": 0}, {"name": "登录态管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "反爬对抗", "created": 0, "pending": 0, "abnormal": 0}, {"name": "采集日志", "created": 0, "pending": 0, "abnormal": 0}, {"name": "模板库", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据标注", "created": 0, "pending": 0, "abnormal": 0}]
  }
})
</script>
