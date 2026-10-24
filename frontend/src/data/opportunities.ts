import type { IconName } from "../components/Icon";

export type OpportunityScenario = { id: string; label: string; effect: string; action: string };
export type BusinessOpportunity = {
  id: string;
  area: string;
  title: string;
  description: string;
  icon: IconName;
  sourceIds: string[];
  scenarios: OpportunityScenario[];
};

// Demonstration hypotheses: opportunities depend on the stated conditions.
export const opportunities: BusinessOpportunity[] = [
  {
    id: "refinancing", area: "Финансы", icon: "percent",
    title: "Оптимизация стоимости финансирования",
    description: "Подготовьте варианты рефинансирования, чтобы пересмотреть условия кредитования при изменении ставки.",
    sourceIds: ["key-rate"],
    scenarios: [
      { id: "unchanged", label: "Ставка сохранится", effect: "Новые займы останутся дорогими. Экономия от рефинансирования пока не подтверждена.", action: "Сравнить предложения банков и сохранить резерв на обслуживание долга." },
      { id: "decrease", label: "Ставка снизится", effect: "Может появиться возможность снизить расходы на финансирование.", action: "Запросить новые условия и учесть комиссии при расчёте экономии." },
    ],
  },
  {
    id: "delivery", area: "Логистика", icon: "briefcase",
    title: "Консолидация поставок",
    description: "На фоне роста тарифов оцените объединение отправок и альтернативные маршруты. Экономию нужно проверить расчётом.",
    sourceIds: ["logistics"],
    scenarios: [
      { id: "separate", label: "Текущая схема", effect: "Рост тарифов увеличивает стоимость отдельных отправок.", action: "Собрать расходы по маршрутам и определить наиболее дорогие направления." },
      { id: "combined", label: "Объединить отправки", effect: "При достаточном объёме совместные отправки могут снизить удельную стоимость доставки.", action: "Запросить расчёт у перевозчиков и проверить сроки и стоимость хранения." },
    ],
  },
  {
    id: "suppliers", area: "Закупки", icon: "building",
    title: "Развитие альтернативных поставок",
    description: "Проверьте доступные аналоги оборудования и возможности расширить круг поставщиков с учётом новых ограничений.",
    sourceIds: ["equipment"],
    scenarios: [
      { id: "current", label: "Текущие поставщики", effect: "Ограничения могут увеличить сроки поставки и сдвинуть модернизацию.", action: "Уточнить наличие, сроки и условия по критичным позициям." },
      { id: "alternative", label: "Альтернативные поставщики", effect: "Подходящие аналоги могут уменьшить зависимость от ограниченных поставок.", action: "Сравнить совместимость, гарантии и полную стоимость владения." },
    ],
  },
];
