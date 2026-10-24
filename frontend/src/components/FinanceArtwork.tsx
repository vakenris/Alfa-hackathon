import { useId } from "react";

/** Decorative, code-native artwork; it contains no financial data. */
export function FinanceArtwork({ variant = "reports" }: { variant?: "reports" | "savings" }) {
  const id = useId().replaceAll(":", "");
  return <svg className={`finance-art finance-art--${variant}`} viewBox="0 0 340 180" aria-hidden="true">
    <defs>
      <linearGradient id={`${id}-paper`} x2=".8" y2="1"><stop stopColor="#fff"/><stop offset="1" stopColor="#e9edf4"/></linearGradient>
      <linearGradient id={`${id}-red`} x2=".8" y2="1"><stop stopColor="#ff8588"/><stop offset=".5" stopColor="#ff3044"/><stop offset="1" stopColor="#d50924"/></linearGradient>
      <filter id={`${id}-shadow`} x="-40%" y="-40%" width="180%" height="190%"><feDropShadow dx="0" dy="9" stdDeviation="8" floodColor="#ac394d" floodOpacity=".15"/></filter>
    </defs>
    <ellipse cx="180" cy="125" rx="145" ry="53" fill="#ffe4e8"/>
    <ellipse cx="230" cy="80" rx="95" ry="53" fill="#ffeff1"/>
    <g filter={`url(#${id}-shadow)`}>
    {variant === "reports" ? <>
      <path d="M224 103 281 35l-26 5 31-23 12 39-17-12-46 75Z" fill={`url(#${id}-red)`}/>
      <g transform="translate(135 25) rotate(7 65 65)"><rect width="101" height="139" rx="14" fill={`url(#${id}-paper)`}/><path d="M17 23h63M17 40h46M17 56h57" stroke="#d6dae5" strokeWidth="7" strokeLinecap="round"/><circle cx="55" cy="104" r="24" fill="#ff6373"/><path d="M55 80v24h24" fill="#d20b29"/></g>
      <g transform="translate(65 19) rotate(-8 50 65)"><rect width="100" height="139" rx="14" fill={`url(#${id}-paper)`}/>{[16,29,43,60].map((h,i)=><rect key={h} x={18+i*17} y={80-h} width="11" height={h} rx="3" fill={`url(#${id}-red)`}/>)}<path d="M18 99h62M18 113h44" stroke="#cfd5e2" strokeWidth="6" strokeLinecap="round"/></g>
    </> : <>
      <g transform="translate(129 19) rotate(12 50 65)"><rect width="100" height="136" rx="17" fill={`url(#${id}-paper)`}/><text x="50" y="95" textAnchor="middle" fontFamily="Arial" fontWeight="700" fontSize="84" fill={`url(#${id}-red)`}>%</text></g>
      {[0,1,2].map(i=><g key={i} transform={`translate(77 ${130-i*17})`}><path d="M0 0h62v15c0 15-62 15-62 0Z" fill={`url(#${id}-red)`}/><ellipse cx="31" rx="31" ry="10" fill="#ffb3ba"/></g>)}
      <g transform="translate(225 77) rotate(9)"><rect width="72" height="85" rx="12" fill={`url(#${id}-paper)`}/><path d="m13 25 13 22 15-7 17 26" fill="none" stroke="#f32740" strokeWidth="5" strokeLinecap="round"/></g>
    </>}
    </g>
  </svg>;
}
