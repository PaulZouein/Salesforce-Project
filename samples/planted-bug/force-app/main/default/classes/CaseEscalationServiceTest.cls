@isTest
private class CaseEscalationServiceTest {

    private static Account createAccount(String name, String tier) {
        Account a = new Account(Name = name, Support_Tier__c = tier);
        insert a;
        return a;
    }

    @isTest
    static void premiumAccountCaseIsHighPriority() {
        Account premium = createAccount('Premium Test Account', 'Premium');

        Test.startTest();
        Case c = new Case(Subject = 'Premium case', AccountId = premium.Id, Priority = 'Medium');
        insert c;
        Test.stopTest();

        c = [SELECT Priority FROM Case WHERE Id = :c.Id];
        System.assertEquals('High', c.Priority, 'A case for a Premium account should be High priority');
    }

    @isTest
    static void standardAccountCaseKeepsPriority() {
        Account standard = createAccount('Standard Test Account', 'Standard');

        Test.startTest();
        Case c = new Case(Subject = 'Standard case', AccountId = standard.Id, Priority = 'Medium');
        insert c;
        Test.stopTest();

        c = [SELECT Priority FROM Case WHERE Id = :c.Id];
        System.assertEquals('Medium', c.Priority, 'A case for a Standard account keeps its priority');
    }
}
