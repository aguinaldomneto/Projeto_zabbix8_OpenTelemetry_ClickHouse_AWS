<?php declare(strict_types = 0);
/**
 * Insights (IA) widget form view.
 *
 * @var CView $this
 * @var array $data
 */

(new CWidgetFormView($data))
	->addField(array_key_exists('groupids', $data['fields'])
		? new CWidgetFieldMultiSelectGroupView($data['fields']['groupids'])
		: null
	)
	->addField(
		new CWidgetFieldTextAreaView($data['fields']['pergunta'])
	)
	->show();
