<?php declare(strict_types = 0);

namespace Modules\Insights\Includes;

use Zabbix\Widgets\CWidgetForm;

use Zabbix\Widgets\Fields\{
	CWidgetFieldMultiSelectGroup,
	CWidgetFieldTextArea
};

/**
 * Insights (IA) widget form.
 */
class WidgetForm extends CWidgetForm {

	public function addFields(): self {
		return $this
			->addField($this->isTemplateDashboard()
				? null
				: new CWidgetFieldMultiSelectGroup('groupids', _('Host groups'))
			)
			->addField(
				new CWidgetFieldTextArea('pergunta', _('Pergunta para a IA (opcional)'))
			);
	}
}
