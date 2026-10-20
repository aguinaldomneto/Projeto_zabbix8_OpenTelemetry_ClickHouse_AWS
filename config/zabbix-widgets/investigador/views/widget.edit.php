<?php declare(strict_types = 0);
/**
 * @var CView $this
 * @var array $data
 */

(new CWidgetFormView($data))
	->addField(array_key_exists('groupids', $data['fields'])
		? new CWidgetFieldMultiSelectGroupView($data['fields']['groupids'])
		: null
	)
	->show();
