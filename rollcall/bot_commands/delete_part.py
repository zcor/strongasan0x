"""
Delete part command - remove a specific part from an attestation
"""
from rollcall.models import Attestation
from asgiref.sync import sync_to_async


async def delete_attestation_part(attestation_id, part_number, user_mapping=None, is_admin=False):
    """
    Delete a specific part from an attestation
    
    Args:
        attestation_id: ID of the parent attestation
        part_number: Part number to delete
        user_mapping: User mapping to verify ownership
        is_admin: Whether user is admin
    
    Returns:
        tuple: (success: bool, message: str)
    """
    def delete_part():
        try:
            parent = Attestation.objects.get(id=attestation_id, parent_attestation__isnull=True)
        except Attestation.DoesNotExist:
            return False, "Attestation not found"
        
        # Check ownership or admin
        if not is_admin:
            if parent.source == 'discord':
                if not parent.discord_user or parent.discord_user.id != user_mapping.id:
                    return False, "You can only delete your own attestation parts"
            else:
                if not parent.telegram_user or parent.telegram_user.id != user_mapping.id:
                    return False, "You can only delete your own attestation parts"
        
        # Find the part to delete
        if part_number == 1:
            # Can't delete the main part, only sub-parts
            return False, "Cannot delete the main attestation part. Delete sub-parts only."
        
        part = Attestation.objects.filter(
            parent_attestation=parent,
            part_number=part_number
        ).first()
        
        if not part:
            return False, f"Part {part_number} not found"
        
        # Delete the part
        part.delete()
        
        # Renumber remaining parts
        remaining_parts = Attestation.objects.filter(
            parent_attestation=parent,
            part_number__gt=part_number
        ).order_by('part_number')
        
        for remaining in remaining_parts:
            remaining.part_number -= 1
            remaining.save()
        
        return True, f"Part {part_number} deleted successfully"
    
    success, message = await sync_to_async(delete_part)()
    return success, message



