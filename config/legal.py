from django.conf import settings
from django.views.generic import TemplateView


class LegalPage(TemplateView):
    page_title = ''

    def get_context_data(self, **kwargs):
        return super().get_context_data(legal=settings.LEGAL, page_title=self.page_title, **kwargs)


privacy = LegalPage.as_view(template_name='legal/privacy.html', page_title='Privacy policy')
terms = LegalPage.as_view(template_name='legal/terms.html', page_title='Terms of service')
