/**
 * Priority rules for cases, based on the support tier of the case's account.
 *
 * Business rule: every case that belongs to a Premium account is High priority.
 */
public with sharing class CaseEscalationService {

    public static final String PREMIUM_TIER = 'Premium';
    public static final String HIGH_PRIORITY = 'High';

    public static void applyPremiumPriority(List<Case> cases) {
        Set<Id> accountIds = new Set<Id>();
        for (Case c : cases) {
            if (c.AccountId != null) {
                accountIds.add(c.AccountId);
            }
        }
        if (accountIds.isEmpty()) {
            return;
        }

        Map<Id, Account> accountsById = new Map<Id, Account>(
            [SELECT Id, Support_Tier__c FROM Account WHERE Id IN :accountIds]
        );

        for (Case c : cases) {
            Account account = accountsById.get(c.AccountId);
            if (account == null || account.Support_Tier__c != PREMIUM_TIER) {
                // Not a premium customer: nothing to do.
                return;
            }
            c.Priority = HIGH_PRIORITY;
        }
    }
}
