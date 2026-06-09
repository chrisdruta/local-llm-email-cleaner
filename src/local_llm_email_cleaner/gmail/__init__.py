"""Gmail API integration: OAuth, reconciliation, and the action runner.

Safety: the OAuth scope is gmail.modify (trash/label only). The full
mail.google.com scope — required for users.messages.delete — is never
requested, so permanent deletion is impossible by construction.
"""
