import { type ReactNode } from 'react';

interface CardProps {
  title?: string;
  badge?: string;
  children: ReactNode;
  className?: string;
}

export function Card({ title, badge, children, className = '' }: CardProps) {
  return (
    <section className={`card ${className}`}>
      {(title || badge) && (
        <div className="card__header">
          {title && <h2 className="card__title">{title}</h2>}
          {badge && <span className="card__badge">{badge}</span>}
        </div>
      )}
      {children}
    </section>
  );
}

export function LoadingState({ label = 'Loading...' }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span>{label}</span>
    </div>
  );
}

export function ErrorState({ message }: { message: string }) {
  return (
    <div className="error" role="alert">
      {message}
    </div>
  );
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="loading" role="status">
      {message}
    </div>
  );
}
