permit (principal, action == Action::"invoke", resource)
when { context.role_permitted && context.same_tenant && context.enabled };

forbid (principal, action == Action::"invoke", resource)
when { !context.enabled || !context.same_tenant };

forbid (principal, action == Action::"invoke", resource)
when { context.risk_class == "financial" && context.amount_cents > context.money_ceiling };

permit (principal, action == Action::"invoke_unattended", resource)
when { context.role_permitted && context.same_tenant && context.enabled &&
       (context.risk_class == "read" || context.risk_class == "internal_write" ||
        context.risk_class == "external_write" || context.risk_class == "financial") };

permit (principal, action == Action::"approve", resource)
when { context.approver_allowed && context.same_tenant && context.enabled };

permit (principal, action == Action::"publish", resource)
when { context.approver_allowed && context.same_tenant && context.enabled };
