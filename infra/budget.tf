# Alerta de facturación: aviso por email al superar (o prever que se supera) el
# umbral mensual. Techo blando; el coste esperado en reposo es <5 €/mes.

resource "aws_budgets_budget" "monthly" {
  name         = "lfdata-monthly"
  budget_type  = "COST"
  limit_amount = var.budget_limit_amount
  limit_unit   = var.budget_currency
  time_unit    = "MONTHLY"

  # Aviso al alcanzar el 80% del gasto real.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_notification_email]
  }

  # Aviso al prever que el mes cerrará por encima del 100%.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_notification_email]
  }
}
