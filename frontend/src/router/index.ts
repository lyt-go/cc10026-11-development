import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import AuditDistributionView from '@/views/AuditDistributionView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/audit-distribution', name: 'audit-distribution', component: AuditDistributionView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
