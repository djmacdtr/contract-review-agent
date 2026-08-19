import { createRouter, createWebHashHistory } from 'vue-router'
import TaskListView from './views/TaskListView.vue'
import DraftReviewView from './views/DraftReviewView.vue'
import FinalCompareView from './views/FinalCompareView.vue'
import TaskDetailView from './views/TaskDetailView.vue'

export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/tasks' },
    { path: '/tasks', component: TaskListView },
    { path: '/tasks/new/draft', component: DraftReviewView },
    { path: '/tasks/new/final', component: FinalCompareView },
    { path: '/tasks/:taskId', component: TaskDetailView },
  ],
})

