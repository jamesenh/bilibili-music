<script setup lang="ts">
/** 展示账号与会话保存路径，将登录和登出结果通知上层。 */
import { ref, watch } from 'vue'
import { X, KeyRound, ShieldAlert, CheckCircle2, LogOut, Loader2, AlertCircle } from 'lucide-vue-next'
import type { UserInfo } from '@/types/music'
import { apiSaveCookie, apiLogout } from '@/services/tauriBridge'

const props = defineProps<{
  isOpen: boolean
  userInfo?: UserInfo | null
  sessionPath?: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'login-success', user: UserInfo): void
  (e: 'logout-success'): void
}>()

const cookieInput = ref('')
const isSaving = ref(false)
const errorMsg = ref('')

watch(
  () => props.isOpen,
  (open) => {
    if (open) {
      errorMsg.value = ''
      cookieInput.value = ''
    }
  }
)

/** 提交输入的Cookie进行校验，成功后通知上层更新账号。 */
async function handleSave() {
  const raw = cookieInput.value.trim()
  if (!raw) {
    errorMsg.value = '请输入 Cookie 内容'
    return
  }

  isSaving.value = true
  errorMsg.value = ''

  try {
    const user = await apiSaveCookie(raw)
    emit('login-success', user)
    emit('close')
  } catch (err: any) {
    errorMsg.value = typeof err === 'string' ? err : (err?.message || '登录失败，请检查 Cookie')
  } finally {
    isSaving.value = false
  }
}

/** 请求清除本地会话，成功后通知上层退出账号。 */
async function handleLogout() {
  isSaving.value = true
  try {
    await apiLogout()
    emit('logout-success')
    emit('close')
  } catch (err: any) {
    console.error('退出失败', err)
  } finally {
    isSaving.value = false
  }
}
</script>

<template>
  <div
    v-if="isOpen"
    class="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4 animate-in fade-in duration-150 select-none"
    @click.self="emit('close')"
  >
    <div
      class="w-full max-w-md bg-surface-panel border border-surface-border rounded-2xl shadow-modal overflow-hidden p-5 flex flex-col gap-4 animate-in zoom-in-95 duration-150"
    >
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <KeyRound class="w-4 h-4 text-bili-hot" />
          <span class="text-sm font-semibold text-white">账号凭据管理</span>
        </div>
        <button
          class="p-1 rounded-lg text-neutral-400 hover:text-white hover:bg-surface-hover transition-colors"
          @click="emit('close')"
        >
          <X class="w-4 h-4" />
        </button>
      </div>

      <!-- 未登录状态 -->
      <div v-if="!userInfo?.is_login" class="flex flex-col gap-3">
        <p class="text-xs text-neutral-400 leading-relaxed">
          粘贴从 B 站网页端复制的完整 Cookie 文本（需包含 <code class="text-neutral-300">SESSDATA</code> 与 <code class="text-neutral-300">bili_jct</code>）。保存后将自动开启高音质与个人收藏夹。
        </p>

        <textarea
          v-model="cookieInput"
          placeholder="SESSDATA=...; bili_jct=...;"
          rows="3"
          class="w-full bg-surface-hover border border-surface-border rounded-xl p-3 text-xs text-neutral-200 placeholder-neutral-500 outline-none focus:border-bili-hot font-mono resize-none"
        ></textarea>

        <!-- 错误提示 -->
        <div
          v-if="errorMsg"
          class="flex items-center gap-2 p-2.5 rounded-xl bg-red-500/10 border border-red-500/30 text-xs text-red-400"
        >
          <AlertCircle class="w-4 h-4 shrink-0" />
          <span>{{ errorMsg }}</span>
        </div>

        <!-- 安全明示 -->
        <div class="flex items-start gap-2 p-2.5 rounded-xl bg-surface-hover/60 border border-surface-border text-[11px] text-neutral-400">
          <ShieldAlert class="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
          <span>
            凭据将以明文 JSON 仅保存在本地设备目录（登出即删文件）：<br />
            <code class="text-neutral-300 font-mono text-[10px] break-all">{{ sessionPath || '%APPDATA%\\BiliMusic\\session.json' }}</code>
          </span>
        </div>

        <div class="flex justify-end gap-2 mt-2">
          <button
            class="px-4 py-2 rounded-xl text-xs bg-surface-hover text-neutral-300 hover:text-white transition-colors"
            @click="emit('close')"
          >
            取消
          </button>
          <button
            :disabled="isSaving"
            class="px-4 py-2 rounded-xl text-xs bg-bili-hot hover:bg-bili-hot/90 text-white font-medium transition-colors shadow-glow flex items-center gap-1.5 disabled:opacity-50"
            @click="handleSave"
          >
            <Loader2 v-if="isSaving" class="w-3.5 h-3.5 animate-spin" />
            <span>{{ isSaving ? '校验并保存中...' : '保存并登录' }}</span>
          </button>
        </div>
      </div>

      <!-- 已登录状态 -->
      <div v-else class="flex flex-col gap-4">
        <div class="flex items-center gap-3 p-3 rounded-xl bg-surface-hover border border-surface-border">
          <div class="w-10 h-10 rounded-full bg-neutral-700 flex items-center justify-center overflow-hidden shrink-0 border border-surface-border">
            <img
              v-if="userInfo.avatar"
              :src="userInfo.avatar"
              :alt="userInfo.uname"
              class="w-full h-full object-cover"
            />
            <span v-else class="text-bili-hot font-bold text-sm">{{ (userInfo.uname || 'U').slice(0, 1) }}</span>
          </div>
          <div class="flex flex-col min-w-0">
            <span class="text-xs font-semibold text-white truncate">{{ userInfo.uname }}</span>
            <span class="text-[10px] text-emerald-400 flex items-center gap-1">
              <CheckCircle2 class="w-3 h-3" />
              已登录有效 (MID: {{ userInfo.mid }})
            </span>
          </div>
        </div>

        <div class="flex items-start gap-2 p-2.5 rounded-xl bg-surface-hover/60 border border-surface-border text-[11px] text-neutral-400">
          <ShieldAlert class="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
          <span>
            当前凭据文件：<br />
            <code class="text-neutral-300 font-mono text-[10px] break-all">{{ sessionPath || '%APPDATA%\\BiliMusic\\session.json' }}</code>
          </span>
        </div>

        <div class="flex justify-between items-center mt-2">
          <span class="text-[11px] text-neutral-500 font-mono">
            session.json 已就绪
          </span>
          <button
            :disabled="isSaving"
            class="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs bg-red-500/10 hover:bg-red-500/20 text-red-400 border border-red-500/30 transition-colors disabled:opacity-50"
            @click="handleLogout"
          >
            <Loader2 v-if="isSaving" class="w-3.5 h-3.5 animate-spin" />
            <LogOut v-else class="w-3.5 h-3.5" />
            <span>退出登录</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
