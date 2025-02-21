from generated.array import Array
from generated.base_struct import BaseStruct
from generated.formats.manis.imports import name_type_map


class SubChunkZt(BaseStruct):

	"""
	arg is chunksizes
	"""

	__name__ = 'SubChunkZt'


	def __init__(self, context, arg=0, template=None, set_default=True):
		super().__init__(context, arg, template, set_default=False)
		self.ref = name_type_map['Empty'](self.context, 0, None)
		self.list_one = Array(self.context, 0, None, (0,), name_type_map['Float'])
		self.list_two = Array(self.context, 0, None, (0,), name_type_map['ElemZt'])
		if set_default:
			self.set_defaults()

	@classmethod
	def _get_attribute_list(cls):
		yield from super()._get_attribute_list()
		yield 'ref', name_type_map['Empty'], (0, None), (False, None), (None, None)
		yield 'list_one', Array, (0, None, (None, 6,), name_type_map['Float']), (False, None), (None, None)
		yield 'list_two', Array, (0, None, (None,), name_type_map['ElemZt']), (False, None), (None, None)

	@classmethod
	def _get_filtered_attribute_list(cls, instance, include_abstract=True):
		yield from super()._get_filtered_attribute_list(instance, include_abstract)
		yield 'ref', name_type_map['Empty'], (0, None), (False, None)
		yield 'list_one', Array, (0, None, (instance.arg.count_b, 6,), name_type_map['Float']), (False, None)
		yield 'list_two', Array, (0, None, (instance.arg.count_b,), name_type_map['ElemZt']), (False, None)
