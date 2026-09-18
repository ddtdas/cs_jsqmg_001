// usePolling：通用轮询组合式函数（后端无 WS /ws/events 时的实时降级通道）。
// 用法: const { data, error, refresh } = usePolling(fetcher, 5000)
import { onBeforeUnmount, ref } from 'vue'

export function usePolling(fetcher, intervalMs = 5000, immediate = true) {
  const data = ref(null)
  const error = ref(null)
  const loading = ref(false)
  let timer = null
  let stopped = false
  let isFirstRun = true

  async function refresh() {
    if (stopped) return
    // 首轮置 loading（初始加载提示）；后续轮询静默刷新，避免整表/整页 v-loading 闪烁
    if (isFirstRun) loading.value = true
    try {
      data.value = await fetcher()
      error.value = null
    } catch (e) {
      error.value = e
    } finally {
      loading.value = false
      isFirstRun = false
    }
  }

  function start() {
    stopped = false
    refresh()
    timer = setInterval(refresh, intervalMs)
  }

  function stop() {
    stopped = true
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  if (immediate) start()
  onBeforeUnmount(stop)

  return { data, error, loading, refresh, start, stop }
}