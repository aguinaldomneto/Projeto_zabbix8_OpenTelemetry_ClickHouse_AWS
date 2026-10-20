<?php declare(strict_types = 0);

namespace Modules\Investigador\Includes;

use Zabbix\Widgets\CWidgetForm;

use Zabbix\Widgets\Fields\CWidgetFieldMultiSelectGroup;

class WidgetForm extends CWidgetForm {

	public function addFields(): self {
		return $this->addField($this->isTemplateDashboard()
			? null
			: new CWidgetFieldMultiSelectGroup('groupids', _('Host groups'))
		);
	}
}
