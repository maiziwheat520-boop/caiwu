import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from 'react'

const classes = (...values: Array<string | undefined>) => values.filter(Boolean).join(' ')

type FieldProps = {
  label: ReactNode
  fieldClassName?: string
}

export function MonthInput({ label, fieldClassName, className, ...props }: FieldProps & Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>) {
  return (
    <label className={classes('temporal-field', fieldClassName)}>
      <span className="temporal-field-label">{label}</span>
      <input {...props} aria-label={props['aria-label'] ?? (typeof label === 'string' ? label : undefined)} className={classes('temporal-control', className)} type="month" />
    </label>
  )
}

export function DateInput({ label, fieldClassName, className, ...props }: FieldProps & Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>) {
  return (
    <label className={classes('temporal-field', fieldClassName)}>
      <span className="temporal-field-label">{label}</span>
      <input {...props} aria-label={props['aria-label'] ?? (typeof label === 'string' ? label : undefined)} className={classes('temporal-control', className)} type="date" />
    </label>
  )
}

export function PeriodSelect({ label, fieldClassName, className, children, ...props }: FieldProps & SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <label className={classes('temporal-field', fieldClassName)}>
      <span className="temporal-field-label">{label}</span>
      <select {...props} aria-label={props['aria-label'] ?? (typeof label === 'string' ? label : undefined)} className={classes('temporal-control', className)}>
        {children}
      </select>
    </label>
  )
}
