<template>
  <section class="page" data-testid="audit-distribution">
    <header class="page-head">
      <div>
        <h2>审计分发</h2>
        <p class="page-desc">
          把已校验的调用审计明细按订阅范围分发给复核责任人：先核对权限，再按请求号关联原始调用，
          共享字段与受控字段分开交付，专有字段按收件方隔离。
        </p>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="loadAll">
      <label class="filter-item">
        <span>调用记录 ID</span>
        <input v-model.number="auditId" data-testid="audit-id" type="number" min="1" placeholder="如 2" />
      </label>
      <button class="btn primary" data-testid="check-permissions" type="submit">核对订阅权限</button>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </form>

    <template v-if="permissions">
      <p class="status-legend">
        请求号 <strong>{{ permissions.request_no }}</strong> · 目标API {{ permissions.api_name }} ·
        审计状态 {{ permissions.audit_status }}
      </p>

      <h3 class="section-title">分发前权限核对</h3>
      <table class="data-table" data-testid="permission-table">
        <thead>
          <tr><th>订阅</th><th>收件方</th><th>可分发</th><th>数据范围</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in permissions.recipients" :key="item.subscription_id">
            <td>{{ item.application_no }}（#{{ item.subscription_id }}）</td>
            <td>{{ item.recipient }}</td>
            <td :data-eligible="item.eligible">{{ item.eligible ? '可分发' : '拒发' }}</td>
            <td>{{ item.in_scope ? '范围内' : '范围外' }}</td>
            <td>{{ item.reasons.length ? item.reasons.join('；') : item.checks.filter((c) => c.passed).map((c) => c.detail).join('；') }}</td>
          </tr>
        </tbody>
      </table>

      <div class="page-actions" style="margin: 12px 0">
        <button class="btn primary" data-testid="distribute" type="button" @click="runDistribute">
          生成并发放数据包
        </button>
      </div>
    </template>

    <template v-if="distribution">
      <p class="status-legend" data-testid="shared-numbers">
        公共数字：请求号 <strong>{{ distribution.shared_numbers.request_no }}</strong>，
        返回行数 <strong>{{ distribution.shared_numbers.returned_rows }}</strong>
      </p>

      <h3 class="section-title">各收件方数据包</h3>
      <div class="package-grid">
        <article
          v-for="pkg in distribution.packages"
          :key="pkg.package_id"
          class="package-card"
          :data-testid="`package-${pkg.subscription_id}`"
        >
          <header class="package-head">
            <strong>{{ pkg.recipient }}</strong>
            <span class="badge">{{ pkg.status }}</span>
          </header>
          <p class="package-meta">
            {{ pkg.application_no }} · 数据包 {{ pkg.package_id }} · 行数 {{ pkg.row_count }}
            <span v-if="pkg.reissue_count">· 已重发 {{ pkg.reissue_count }} 次</span>
          </p>
          <table class="data-table compact">
            <thead><tr><th>字段</th><th>类型</th><th>值</th></tr></thead>
            <tbody>
              <tr v-for="column in pkg.columns" :key="column.field">
                <td>{{ column.field }}</td>
                <td>{{ column.class }}</td>
                <td>
                  {{ column.value }}
                  <span v-if="column.masked" class="mask-tag">脱敏·{{ column.mask_algorithm }}</span>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="pkg.withheld_fields.length" class="withheld">
            未交付：{{ pkg.withheld_fields.map((w) => w.field).join('、') }}
          </p>
          <p class="package-meta">
            对账：请求号 {{ pkg.reconciliation.request_no }} · 返回 {{ pkg.reconciliation.returned_rows }} 行 ·
            用量 {{ pkg.reconciliation.usage_record_no ?? '无记录' }} {{ pkg.reconciliation.usage_data_rows }} 行 ·
            差异 {{ pkg.reconciliation.difference }}
          </p>
          <button class="btn" type="button" :data-testid="`reissue-${pkg.subscription_id}`" @click="runReissue(pkg.request_no, pkg.subscription_id)">
            按请求号重发
          </button>
        </article>
      </div>

      <p v-if="distribution.rejected.length" class="status-legend" data-testid="rejected">
        拒发：{{ distribution.rejected.map((r) => `${r.recipient}（${r.reasons.join('；')}）`).join(' / ') }}
      </p>
    </template>

    <template v-if="comparison">
      <h3 class="section-title">双订阅复核对照</h3>
      <p class="status-legend" data-testid="compare-summary">
        同一请求号：{{ comparison.reconciliation.same_request_no ? '是' : '否' }} ·
        同一返回行数：{{ comparison.reconciliation.same_returned_rows ? '是' : '否' }}
        （{{ comparison.reconciliation.returned_rows }} 行）·
        专有字段隔离：{{ comparison.isolated ? '通过' : '存在串字段' }}
      </p>
      <table class="data-table" data-testid="compare-table">
        <thead>
          <tr><th>收件方</th><th>请求号</th><th>返回行数</th><th>可用于对账的列</th><th>专有列</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in comparison.recipients" :key="row.subscription_id">
            <td>{{ row.recipient }}</td>
            <td>{{ row.request_no }}</td>
            <td>{{ row.returned_rows }}</td>
            <td>{{ row.usable_columns.join('、') }}</td>
            <td>{{ row.controlled_columns.join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p class="status-legend">
        共同列：{{ comparison.overlap_columns.join('、') }}
      </p>
      <p class="status-legend">
        各自独有：
        <span v-for="(cols, sid) in comparison.unique_columns" :key="sid">订阅 {{ sid }}：{{ cols.join('、') }}；</span>
      </p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  compareSubscriptions,
  distributeAudit,
  loadPermissions,
  reissuePackage,
} from '@/api/backend-service'
import type { CompareResult, DistributeResult, PermissionsResult } from '@/api/backend-service'

const auditId = ref(2)
const permissions = ref<PermissionsResult | null>(null)
const distribution = ref<DistributeResult | null>(null)
const comparison = ref<CompareResult | null>(null)
const errorMessage = ref('')

function fail(error: unknown) {
  errorMessage.value = error instanceof Error ? error.message : '操作失败'
}

async function loadAll() {
  errorMessage.value = ''
  distribution.value = null
  comparison.value = null
  try {
    permissions.value = await loadPermissions(auditId.value)
    const eligible = permissions.value.recipients.filter((r) => r.eligible)
    if (eligible.length >= 2) {
      comparison.value = await compareSubscriptions(
        permissions.value.request_no,
        eligible.slice(0, 2).map((r) => r.subscription_id),
      )
    }
  } catch (error) {
    permissions.value = null
    fail(error)
  }
}

async function runDistribute() {
  errorMessage.value = ''
  try {
    distribution.value = await distributeAudit(auditId.value)
  } catch (error) {
    fail(error)
  }
}

async function runReissue(requestNo: string, subscriptionId: number) {
  errorMessage.value = ''
  try {
    await reissuePackage(requestNo, subscriptionId)
    await runDistribute()
  } catch (error) {
    fail(error)
  }
}
</script>
