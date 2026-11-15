/**
 * Dispatches Case trigger events to the services that own the business rules.
 */
public with sharing class CaseTriggerHandler {

    public static void beforeInsert(List<Case> newCases) {
        CaseEscalationService.applyPremiumPriority(newCases);
    }

    public static void beforeUpdate(List<Case> newCases, Map<Id, Case> oldCasesById) {
        List<Case> movedToAnotherAccount = new List<Case>();
        for (Case c : newCases) {
            Case oldCase = oldCasesById.get(c.Id);
            if (c.AccountId != oldCase.AccountId) {
                movedToAnotherAccount.add(c);
            }
        }
        if (!movedToAnotherAccount.isEmpty()) {
            CaseEscalationService.applyPremiumPriority(movedToAnotherAccount);
        }
    }
}
