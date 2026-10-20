/**
 * Insights (IA): botão "Analisar com IA" chama o ia-assistente (via a
 * action PHP widget.insights.analyze) e mostra o resumo do Gemini aqui
 * mesmo, sem sair do dashboard.
 */
class CWidgetInsights extends CWidget {

	setContents(response) {
		super.setContents(response);

		const botao = this._body.querySelector('.js-insights-analyze');
		const resultado = this._body.querySelector('.insights-resultado');

		if (!botao || !resultado) {
			return;
		}

		botao.addEventListener('click', () => {
			botao.disabled = true;
			resultado.textContent = t('Analisando...');

			const url = new Curl('zabbix.php');
			url.setArgument('action', 'widget.insights.analyze');

			jQuery.ajax({
				url: url.getUrl(),
				method: 'POST',
				data: {
					groupids: this.getFields().groupids || [],
					pergunta: this.getFields().pergunta || '',
					[CSRF_TOKEN_NAME]: botao.dataset.csrf || ''
				},
				dataType: 'json',
				success: (data) => {
					// textContent: nunca interpreta HTML vindo do serviço/modelo.
					resultado.textContent = data.erro ? data.erro : data.resposta;
				},
				error: () => {
					resultado.textContent = t('Falha ao chamar a IA.');
				},
				complete: () => {
					botao.disabled = false;
				}
			});
		});
	}
}
