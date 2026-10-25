import type { IconName } from "./components/Icon";

export interface Recommendation { title: string; description: string; icon: IconName }

export function eventRecommendations(text: string): Recommendation[] {
  const verbs = "(?:провести|пересмотреть|запросить|проверить|оценить|обновить|актуализировать|согласовать|сравнить|подготовить|обсудить|рассмотреть|определить|уточнить|снизить|увеличить|оптимизировать)";
  const separator = new RegExp(`(?:^|\\s)\\d+[.)]\\s+|\\n+|;\\s*|,\\s*(?=${verbs}(?=\\s|$))|\\s+и\\s+(?=${verbs}(?=\\s|$))`, "iu");
  return text.split(separator).map(part => part.replace(/^[•—-]\s*/, "").trim()).filter(Boolean).map(part => {
    const title = part.charAt(0).toUpperCase() + part.slice(1).replace(/[.,;]+$/, "");
    if (/^запросить(?:\s|$)/i.test(part)) return {title, icon: "briefcase", description: "Обсудить возможные условия и получить предложение от банка."};
    if (/пересмотр|лимит|договор/i.test(part)) return {title, icon: "document", description: "Проверить текущие условия и при необходимости инициировать пересмотр."};
    if (/комитет|обсуд|согласов/i.test(part)) return {title, icon: "chart", description: "Оценить текущую ситуацию и согласовать дальнейшие шаги."};
    return {title, icon: "chart", description: "Уточнить детали рекомендации с учётом ситуации компании."};
  });
}
