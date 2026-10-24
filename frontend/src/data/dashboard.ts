export type Tone = "red" | "orange" | "green" | "blue" | "purple";
export interface NewsItem {
  id: string;
  category: string;
  time: string;
  title: string;
  image: string;
  imageAlt: string;
  impact: "high" | "medium";
  description: string;
  takeaway: string;
  effects: { title: string; description: string; status: string }[];
}
export const companies = [
  "ООО «ТехноПром»",
  "АО «ТехноЛогистика»",
  "ООО «ПромСнаб»",
  "ПАО «ЭнергоСервис»",
];
export const roles = [
  "Финансовый директор",
  "Генеральный директор",
  "Бизнес-аналитик",
];
export const news: NewsItem[] = [
  {
    id: "key-rate",
    category: "Макроэкономика",
    time: "Сегодня, 12:30",
    title: "Банк России сохранил ключевую ставку на уровне 14%",
    image: "/images/bank.png",
    imageAlt: "Здание Банка России и российский флаг",
    impact: "high",
    description:
      "Регулятор отметил, что текущее инфляционное давление остаётся высоким, но динамика цен постепенно замедляется.",
    takeaway:
      "Сохранение ставки на уровне 14% поддерживает стабильность финансового рынка, но сохраняет высокую стоимость заимствований для бизнеса.",
    effects: [
      {
        title: "Стоимость финансирования",
        description: "Кредиты и новые займы",
        status: "Высокая",
      },
      {
        title: "Обслуживание долга",
        description: "Текущие обязательства",
        status: "Под давлением",
      },
      {
        title: "Инвестиционный бюджет",
        description: "Новые проекты и расширение",
        status: "Требует оценки",
      },
    ],
  },
  {
    id: "logistics",
    category: "Логистика",
    time: "Сегодня, 10:15",
    title: "Рост стоимости грузоперевозок на 12% в ключевых регионах",
    image: "/images/truck.png",
    imageAlt: "Грузовик на трассе на закате",
    impact: "medium",
    description:
      "Повышение тарифов увеличивает расходы компаний на доставку сырья и готовой продукции.",
    takeaway:
      "Повышение тарифов увеличивает транспортную составляющую себестоимости. Масштаб последствий зависит от маршрутов и договорных условий.",
    effects: [
      {
        title: "Транспортные расходы",
        description: "Доставка сырья и продукции",
        status: "Растут",
      },
      {
        title: "Маржинальность",
        description: "Себестоимость продукции",
        status: "Под давлением",
      },
      {
        title: "Маршруты поставок",
        description: "Альтернативные перевозчики",
        status: "Требует оценки",
      },
    ],
  },
  {
    id: "equipment",
    category: "Промышленность",
    time: "Вчера, 18:20",
    title: "Новые ограничения на импорт промышленного оборудования",
    image: "/images/industry.png",
    imageAlt: "Промышленные трубы с красным вентилем",
    impact: "high",
    description:
      "Изменения в условиях импорта могут повлиять на сроки поставки оборудования и планы модернизации производства.",
    takeaway:
      "Ограничения могут повлиять на доступность оборудования и сроки модернизации. Применимость зависит от категории продукции и условий поставки.",
    effects: [
      {
        title: "Закупка оборудования",
        description: "Поставки и комплектующие",
        status: "Ограничена",
      },
      {
        title: "Сроки модернизации",
        description: "Производственные проекты",
        status: "Под давлением",
      },
      {
        title: "Инвестиционный бюджет",
        description: "Замена поставщиков",
        status: "Требует оценки",
      },
    ],
  },
];
export const businessEvents = [
  {
    id: "rate-review",
    sourceIds: ["key-rate"],
    date: "26 октября",
    title: "Оценить влияние ключевой ставки",
  },
  {
    id: "transport",
    sourceIds: ["logistics"],
    date: "27 октября",
    title: "Проверить тарифы перевозчиков",
  },
  {
    id: "suppliers",
    sourceIds: ["equipment"],
    date: "28 октября",
    title: "Актуализировать список поставщиков",
  },
  {
    id: "investments",
    sourceIds: ["key-rate", "equipment"],
    date: "29 октября",
    title: "Пересмотреть инвестиционный бюджет",
  },
  {
    id: "finance-plan",
    sourceIds: ["key-rate", "logistics", "equipment"],
    date: "30 октября",
    title: "Обсудить финансовый план с командой",
  },
];
export const newsTopics = [
  {
    id: "all",
    label: "Все новости",
    category: undefined as string | undefined,
  },
  ...Array.from(new Set(news.map((item) => item.category))).map(
    (category, index) => ({ id: `topic-${index}`, label: category, category }),
  ),
];
