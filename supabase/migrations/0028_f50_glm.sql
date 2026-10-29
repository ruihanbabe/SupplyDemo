-- F50: explicit GLM provider, never use arbitrary custom endpoints.
begin;
alter table app.model_policy drop constraint if exists model_policy_provider_check;
alter table app.model_policy add constraint model_policy_provider_check check(provider in ('replay','openai','anthropic','custom','zhipu'));
commit;
