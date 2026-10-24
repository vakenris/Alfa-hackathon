import { useState } from "react";
import type { FormEvent } from "react";
import { Icon } from "../components/Icon";
import { Badge, IconBubble, Modal, Panel } from "../components/ui";
import { companies } from "../data/dashboard";
export interface Profile {
  name: string;
  position: string;
  email: string;
  phone: string;
}
export function AccountPage({
  profile,
  setProfile,
  company,
  setCompany,
}: {
  profile: Profile;
  setProfile: (p: Profile) => void;
  company: string;
  setCompany: (c: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(profile);
  const [saved, setSaved] = useState(false);
  function save(e: FormEvent) {
    e.preventDefault();
    if (!draft.name.trim() || !draft.position.trim()) return;
    setProfile({
      ...draft,
      name: draft.name.trim(),
      position: draft.position.trim(),
    });
    setEditing(false);
    setSaved(true);
  }
  return (
    <div className="account-page">
      <header className="page-header">
        <h1>Личный кабинет</h1>
        <p className="page-subtitle">
          Управляйте профилем, доступами и компаниями.
        </p>
      </header>
      <div className="account-grid">
        <Panel className="profile-panel">
          <h2>Профиль</h2>
          <p className="panel-subtitle">
            Ваши персональные данные и контактная информация.
          </p>
          <div className="profile-info">
            <div className="avatar">
              <Icon name="user" size={54} />
            </div>
            <div className="profile-details">
              <h3>{profile.name}</h3>
              <p className="position">{profile.position}</p>
              <a href={`mailto:${profile.email}`}>
                <Icon name="mail" />
                {profile.email}
              </a>
              <a href={`tel:${profile.phone.replace(/[^+0-9]/g, "")}`}>
                <Icon name="phone" />
                {profile.phone}
              </a>
            </div>
          </div>
          <div className="profile-actions">
            <button
              className="primary-button"
              onClick={() => {
                setDraft(profile);
                setEditing(true);
                setSaved(false);
              }}
            >
              <Icon name="edit" size={22} />
              Редактировать профиль
            </button>
            {saved && (
              <p className="saved-message" role="status">
                Профиль сохранён
              </p>
            )}
          </div>
        </Panel>
        <Panel className="companies-panel">
          <h2>Доступные компании</h2>
          <p className="panel-subtitle">Компании, с которыми вы работаете.</p>
          <div className="companies-list">
            {companies.map((c) => (
              <button
                key={c}
                className={`company-row ${company === c ? "selected" : ""}`}
                onClick={() => setCompany(c)}
                aria-pressed={company === c}
              >
                <IconBubble
                  name="building"
                  tone={company === c ? "red" : "blue"}
                />
                <span>{c}</span>
                {company === c && <Badge tone="green">Активна</Badge>}
                <Icon name="right" size={20} />
              </button>
            ))}
          </div>
        </Panel>
      </div>
      {editing && (
        <Modal title="Редактировать профиль" onClose={() => setEditing(false)}>
          <form className="profile-form" onSubmit={save}>
            {(
              [
                { key: "name", label: "Имя и фамилия", type: "text" },
                { key: "position", label: "Должность", type: "text" },
                { key: "email", label: "Электронная почта", type: "email" },
                { key: "phone", label: "Телефон", type: "tel" },
              ] as const
            ).map((f) => (
              <label key={f.key}>
                {f.label}
                <input
                  required
                  type={f.type}
                  value={draft[f.key]}
                  onChange={(e) =>
                    setDraft({ ...draft, [f.key]: e.target.value })
                  }
                />
              </label>
            ))}
            <div className="form-actions">
              <button
                type="button"
                className="secondary-button"
                onClick={() => setEditing(false)}
              >
                Отмена
              </button>
              <button className="primary-button" type="submit">
                Сохранить изменения
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
