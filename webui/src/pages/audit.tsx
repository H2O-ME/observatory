import { W2Notice } from "@/components/w2-notice";

/**
 * Audit log (W2 view rebuild).
 *
 * Route and navigation entry stay. The W2 version must debounce the action
 * filter: the 0.2.x page put the raw input into the query key, so every
 * keystroke produced a request.
 */
export function AuditPage() {
  return (
    <W2Notice
      title="审计日志（W2 重建）"
      description="审计数据仍由服务端提供；列表、过滤与分页在 W2 波次重建。"
      items={[
        "动作过滤将做输入防抖，避免每敲一个字符请求一次",
        "时间戳一律格式化为服务端时间，不使用浏览器时钟推算",
        "detail 展开与分页保持 URL 无关的本地状态",
      ]}
      testId="audit-w2"
    />
  );
}